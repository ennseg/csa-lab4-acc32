import struct
from pathlib import Path

import config
import isa


def encode(opcode, mode, operand):
    return (opcode << 27) | (mode << 25) | (operand & 0x1FFFFFF)


def write_binary(instructions, path, data_section=None):
    if data_section is None:
        data_section = []
    with open(path, "wb") as f:
        f.write(struct.pack(">I", len(instructions)))
        for word in instructions:
            f.write(struct.pack(">I", word))
        f.write(struct.pack(">I", len(data_section)))
        for addr, value in data_section:
            f.write(struct.pack(">I", addr))
            f.write(struct.pack(">I", value & 0xFFFFFFFF))


def read_binary(path):
    with open(path, "rb") as f:
        n_instr = struct.unpack(">I", f.read(4))[0]
        instructions = [struct.unpack(">I", f.read(4))[0] for _ in range(n_instr)]
        n_data = struct.unpack(">I", f.read(4))[0]
        data_section = []
        for _ in range(n_data):
            addr = struct.unpack(">I", f.read(4))[0]
            value = struct.unpack(">I", f.read(4))[0]
            if value & 0x80000000:
                value -= 0x100000000
            data_section.append((addr, value))
    return instructions, data_section


class Translator:
    TMP_BASE = config.TMP_BASE
    PTR_TMP = config.PTR_TMP
    XTR_TMP = config.XTR_TMP
    ZERO_CELL = config.ZERO_CELL

    def __init__(self):
        self.instructions = []
        self.variables = {}
        self.word_addrs = {}
        self.constants = {}
        self.data_section = []
        self.data_ptr = 0
        self.stack_depth = 0
        self.patches = []

    def _check_name_free(self, name):
        if name in self.variables or name in self.constants or name in self.word_addrs:
            raise ValueError(f"name '{name}' already defined")

    def dump_text(self, path):
        with open(path, "w") as f:
            for addr, word in enumerate(self.instructions):
                opcode = (word >> 27) & 0x1F
                mode = (word >> 25) & 0x03
                operand = word & 0x1FFFFFF
                if operand & 0x1000000:
                    operand -= 0x2000000

                op_name = isa.OPCODE_NAMES.get(opcode, "???")

                if mode == isa.MODE_IMM:
                    arg = f"#{operand}"
                elif mode == isa.MODE_INDIRECT:
                    arg = f"@{operand}"
                else:
                    arg = f"{operand}"

                comment = self._describe(op_name, mode, operand)

                f.write(f"{addr:04X} - {word:08X} - {op_name} {arg}  ; {comment}\n")

    def _describe(self, op_name, mode, operand):
        if mode == isa.MODE_IMM:
            src = f"#{operand}"
        elif mode == isa.MODE_INDIRECT:
            src = f"mem[mem[{operand}]]"
        else:
            src = f"mem[{operand}]"

        descriptions = {
            "LOAD": f"AC <- {src}",
            "STORE": f"{src} <- AC",
            "ADD": f"AC <- AC + {src}",
            "SUB": f"AC <- AC - {src}",
            "MUL": f"AC <- AC * {src}",
            "DIV": f"AC <- AC / {src}",
            "AND": f"AC <- AC & {src}",
            "OR": f"AC <- AC | {src}",
            "NOT": "AC <- ~AC",
            "INC": "AC <- AC + 1",
            "DEC": "AC <- AC - 1",
            "CMP": f"flags <- AC - {src}",
            "JMP": f"IP <- {operand}",
            "JZ": f"if Z: IP <- {operand}",
            "JN": f"if N: IP <- {operand}",
            "JNZ": f"if not Z: IP <- {operand}",
            "JNN": f"if not N: IP <- {operand}",
            "PUSH": "mem[SP--] <- AC",
            "POP": "AC <- mem[++SP]",
            "CALL": f"call {operand}",
            "RET": "return",
            "IN": f"AC <- port[{operand}]",
            "OUT": f"port[{operand}] <- AC",
            "IRET": "interrupt return",
            "HALT": "stop",
        }
        return descriptions.get(op_name, "")

    def emit(self, opcode, mode=isa.MODE_DIRECT, operand=0):
        addr = len(self.instructions)
        self.instructions.append(encode(opcode, mode, operand))
        return addr

    def patch(self, addr, operand):
        word = self.instructions[addr]
        opcode = (word >> 27) & 0x1F
        mode = (word >> 25) & 0x03
        self.instructions[addr] = encode(opcode, mode, operand)

    def tmp_addr(self, depth):
        return self.TMP_BASE + depth

    def vstack_push(self):
        if self.stack_depth > 0:
            self.emit(isa.STORE, isa.MODE_DIRECT, self.tmp_addr(self.stack_depth - 1))
        self.stack_depth += 1

    def vstack_pop(self):
        self.stack_depth -= 1

    def vstack_restore(self):
        self.stack_depth -= 1
        if self.stack_depth > 0:
            self.emit(isa.LOAD, isa.MODE_DIRECT, self.tmp_addr(self.stack_depth - 1))

    def compile_tokens(self, tokens):
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            i += 1

            if self._is_number(tok):
                if i < len(tokens) and tokens[i] == "constant":
                    value = int(tok)
                    name = tokens[i + 1]
                    i += 2
                    self._check_name_free(name)
                    addr = self.data_ptr
                    self.data_ptr += 1
                    self.constants[name] = addr
                    self.data_section.append((addr, value))
                else:
                    self.vstack_push()
                    self.emit(isa.LOAD, isa.MODE_IMM, int(tok))

            elif tok == "variable":
                name = tokens[i]
                i += 1
                self._check_name_free(name)
                addr = self.data_ptr
                self.variables[name] = addr
                self.data_ptr += 1

            elif tok == "constant":
                raise ValueError("constant must follow an immediate value")

            elif tok == "@":
                self.emit(isa.STORE, isa.MODE_DIRECT, self.PTR_TMP)
                self.emit(isa.LOAD, isa.MODE_INDIRECT, self.PTR_TMP)

            elif tok == "!":
                self.emit(isa.STORE, isa.MODE_DIRECT, self.PTR_TMP)
                val_tmp = self.tmp_addr(self.stack_depth - 2)
                self.emit(isa.LOAD, isa.MODE_DIRECT, val_tmp)
                self.emit(isa.STORE, isa.MODE_INDIRECT, self.PTR_TMP)
                self.stack_depth -= 2
                if self.stack_depth > 0:
                    self.emit(isa.LOAD, isa.MODE_DIRECT, self.tmp_addr(self.stack_depth - 1))

            elif tok in self.variables:
                self.vstack_push()
                self.emit(isa.LOAD, isa.MODE_IMM, self.variables[tok])

            elif tok in self.constants:
                self.vstack_push()
                self.emit(isa.LOAD, isa.MODE_DIRECT, self.constants[tok])

            elif tok == "+":
                self._compile_binop(isa.ADD)
            elif tok == "+c":
                self._compile_binop(isa.ADC)
            elif tok == "-":
                self._compile_binop(isa.SUB)
            elif tok == "*":
                self._compile_binop(isa.MUL)
            elif tok == "/":
                self._compile_binop(isa.DIV)
            elif tok == "and":
                self._compile_binop(isa.AND)
            elif tok == "or":
                self._compile_binop(isa.OR)
            elif tok == "not":
                self.emit(isa.NOT)

            elif tok == "=":
                self._compile_cmp("=")
            elif tok == "<":
                self._compile_cmp("<")
            elif tok == ">":
                self._compile_cmp(">")

            elif tok == "dup":
                if self.stack_depth > 0:
                    tmp = self.tmp_addr(self.stack_depth - 1)
                    if config.SUPERSCALAR:
                        self.emit(isa.STORE, isa.MODE_DIRECT, tmp)
                        self.emit(isa.ADD, isa.MODE_DIRECT, self.ZERO_CELL)
                        self.emit(isa.LOAD, isa.MODE_DIRECT, tmp)
                    else:
                        self.emit(isa.STORE, isa.MODE_DIRECT, tmp)
                self.stack_depth += 1

            elif tok == "drop":
                self.stack_depth -= 1
                if self.stack_depth > 0:
                    self.emit(isa.LOAD, isa.MODE_DIRECT, self.tmp_addr(self.stack_depth - 1))

            elif tok == "swap":
                tmp_a = self.tmp_addr(self.stack_depth)
                tmp_b = self.tmp_addr(self.stack_depth - 1)
                self.emit(isa.STORE, isa.MODE_DIRECT, tmp_a)
                self.emit(isa.LOAD, isa.MODE_DIRECT, tmp_b)
                self.emit(isa.STORE, isa.MODE_DIRECT, tmp_b)
                self.emit(isa.LOAD, isa.MODE_DIRECT, tmp_a)

            elif tok == "over":
                second = self.tmp_addr(self.stack_depth - 1)
                self.vstack_push()
                self.emit(isa.LOAD, isa.MODE_DIRECT, second)

            elif tok == "emit" or tok == ".":
                self.emit(isa.OUT, isa.MODE_DIRECT, isa.PORT_STDOUT)
                self.vstack_pop()

            elif tok == "key":
                self.vstack_push()
                self.emit(isa.IN, isa.MODE_DIRECT, isa.PORT_STDIN)

            elif tok == "iret":
                self.emit(isa.IRET)

            elif tok == 's"':
                string = ""
                while i < len(tokens):
                    t = tokens[i]
                    i += 1
                    if t.endswith('"'):
                        string += t[:-1]
                        break
                    string += t + " "
                i = self._compile_string(string, tokens, i)

            elif tok == '."':
                string = ""
                while i < len(tokens):
                    t = tokens[i]
                    i += 1
                    if t.endswith('"'):
                        string += t[:-1]
                        break
                    string += t + " "
                self._compile_print_string(string)

            elif tok == "IF":
                i = self._compile_if(tokens, i)

            elif tok == "BEGIN":
                i = self._compile_begin(tokens, i)

            elif tok == ":":
                i = self._compile_word_def(tokens, i)

            elif tok == ";":
                self.emit(isa.RET)

            elif tok == "'":
                name = tokens[i]
                i += 1
                if name in self.word_addrs:
                    self.vstack_push()
                    self.emit(isa.LOAD, isa.MODE_IMM, self.word_addrs[name])
                else:
                    self.vstack_push()
                    patch_addr = self.emit(isa.LOAD, isa.MODE_IMM, 0)
                    self.patches.append((patch_addr, name))

            elif tok == "execute":
                self.emit(isa.STORE, isa.MODE_DIRECT, self.XTR_TMP)
                self.vstack_restore()
                self.emit(isa.CALL, isa.MODE_INDIRECT, self.XTR_TMP)

            elif tok in self.word_addrs:
                self.emit(isa.CALL, isa.MODE_DIRECT, self.word_addrs[tok])

            elif tok == "type":
                self._compile_type()

            elif tok == "set-irq-handler":
                if self.instructions and (self.instructions[-1] >> 27) & 0x1F == isa.CALL:
                    handler_addr = self.instructions[-1] & 0x1FFFFFF
                    self.instructions.pop()
                    self.patch(0, handler_addr)
                else:
                    raise ValueError("set-irq-handler must follow a word reference")

            elif tok == "halt":
                self.emit(isa.HALT)

            else:
                raise ValueError(f"Unknown token: '{tok}'")

        return i

    def _is_number(self, tok):
        try:
            int(tok)
            return True
        except ValueError:
            return False

    def _compile_binop(self, opcode):
        b_tmp = self.tmp_addr(self.stack_depth - 1)
        a_tmp = self.tmp_addr(self.stack_depth - 2)
        self.emit(isa.STORE, isa.MODE_DIRECT, b_tmp)
        self.emit(isa.LOAD, isa.MODE_DIRECT, a_tmp)
        self.emit(opcode, isa.MODE_DIRECT, b_tmp)
        self.stack_depth -= 1

    def _compile_cmp(self, op):
        b_tmp = self.tmp_addr(self.stack_depth - 1)
        a_tmp = self.tmp_addr(self.stack_depth - 2)
        self.emit(isa.STORE, isa.MODE_DIRECT, b_tmp)
        self.emit(isa.LOAD, isa.MODE_DIRECT, a_tmp)
        self.emit(isa.CMP, isa.MODE_DIRECT, b_tmp)

        if op == "=":
            jmp_true = self.emit(isa.JZ, isa.MODE_DIRECT, 0)
        elif op == "<":
            jmp_true = self.emit(isa.JN, isa.MODE_DIRECT, 0)
        elif op == ">":
            jz_skip = self.emit(isa.JZ, isa.MODE_DIRECT, 0)
            jmp_true = self.emit(isa.JNN, isa.MODE_DIRECT, 0)
            self.patch(jz_skip, len(self.instructions))

        self.emit(isa.LOAD, isa.MODE_IMM, 0)
        jmp_end = self.emit(isa.JMP, isa.MODE_DIRECT, 0)

        true_addr = len(self.instructions)
        self.patch(jmp_true, true_addr)
        self.emit(isa.LOAD, isa.MODE_IMM, -1)

        end_addr = len(self.instructions)
        self.patch(jmp_end, end_addr)

        self.stack_depth -= 1

    def _compile_if(self, tokens, i):
        jz_addr = self.emit(isa.JZ, isa.MODE_DIRECT, 0)
        self.vstack_restore()

        while i < len(tokens):
            tok = tokens[i]
            i += 1

            if tok == "THEN":
                then_addr = len(self.instructions)
                self.patch(jz_addr, then_addr)
                if self.stack_depth > 0:
                    self.emit(isa.LOAD, isa.MODE_DIRECT, self.tmp_addr(self.stack_depth - 1))
                break

            elif tok == "ELSE":
                jmp_addr = self.emit(isa.JMP, isa.MODE_DIRECT, 0)
                else_addr = len(self.instructions)
                self.patch(jz_addr, else_addr)
                while i < len(tokens):
                    tok2 = tokens[i]
                    i += 1

                    if tok2 == "THEN":
                        then_addr = len(self.instructions)
                        self.patch(jmp_addr, then_addr)
                        break
                    else:
                        i = self._compile_one_token(tok2, tokens, i)
                break
            else:
                i = self._compile_one_token(tok, tokens, i)

        return i

    def _compile_begin(self, tokens, i):
        begin_addr = len(self.instructions)

        while i < len(tokens):
            tok = tokens[i]
            i += 1

            if tok == "UNTIL":
                self.emit(isa.JZ, isa.MODE_DIRECT, begin_addr)
                self.vstack_pop()
                break

            elif tok == "WHILE":
                jz_addr = self.emit(isa.JZ, isa.MODE_DIRECT, 0)
                self.vstack_pop()
                while i < len(tokens):
                    tok2 = tokens[i]
                    i += 1
                    if tok2 == "REPEAT":
                        self.emit(isa.JMP, isa.MODE_DIRECT, begin_addr)
                        after_addr = len(self.instructions)
                        self.patch(jz_addr, after_addr)
                        break
                    else:
                        i = self._compile_one_token(tok2, tokens, i)
                break
            else:
                i = self._compile_one_token(tok, tokens, i)

        return i

    def _compile_word_def(self, tokens, i):
        name = tokens[i]
        i += 1

        jmp_over = self.emit(isa.JMP, isa.MODE_DIRECT, 0)
        word_addr = len(self.instructions)
        self.word_addrs[name] = word_addr

        saved_depth = self.stack_depth
        self.stack_depth = 1

        while i < len(tokens):
            tok = tokens[i]
            i += 1
            if tok == ";":
                self.emit(isa.RET)
                break
            else:
                i = self._compile_one_token(tok, tokens, i)

        self.stack_depth = saved_depth

        after_addr = len(self.instructions)
        self.patch(jmp_over, after_addr)
        return i

    def _compile_string(self, string, tokens, i):
        str_addr = self.data_ptr
        self.data_ptr += 1 + len(string)
        self.data_section.append((str_addr, len(string)))
        for idx, c in enumerate(string):
            self.data_section.append((str_addr + 1 + idx, ord(c)))
        self.vstack_push()
        self.emit(isa.LOAD, isa.MODE_IMM, str_addr)
        return i

    def _compile_print_string(self, string):
        for c in string:
            self.emit(isa.LOAD, isa.MODE_IMM, ord(c))
            self.emit(isa.OUT, isa.MODE_DIRECT, isa.PORT_STDOUT)

    def _compile_one_token(self, tok, tokens, i):
        if tok == "IF":
            return self._compile_if(tokens, i)
        elif tok == "BEGIN":
            return self._compile_begin(tokens, i)
        elif tok == ":":
            return self._compile_word_def(tokens, i)
        elif tok == 's"':
            string = ""
            while i < len(tokens):
                t = tokens[i]
                i += 1
                if t.endswith('"'):
                    string += t[:-1]
                    break
                string += t + " "
            self._compile_string(string, tokens, i)
            return i
        elif tok == '."':
            string = ""
            while i < len(tokens):
                t = tokens[i]
                i += 1
                if t.endswith('"'):
                    string += t[:-1]
                    break
                string += t + " "
            self._compile_print_string(string)
            return i
        elif tok == "'":
            name = tokens[i]
            i += 1
            if name in self.word_addrs:
                self.vstack_push()
                self.emit(isa.LOAD, isa.MODE_IMM, self.word_addrs[name])
            else:
                self.vstack_push()
                patch_addr = self.emit(isa.LOAD, isa.MODE_IMM, 0)
                self.patches.append((patch_addr, name))
            return i
        elif tok == "variable":
            name = tokens[i]
            i += 1
            self._check_name_free(name)
            addr = self.data_ptr
            self.variables[name] = addr
            self.data_ptr += 1
            return i
        elif tok == "constant":
            raise ValueError("constant must be declared in the main program, not inside a word")
        elif tok == "set-irq-handler":
            if self.instructions and (self.instructions[-1] >> 27) & 0x1F == isa.CALL:
                handler_addr = self.instructions[-1] & 0x1FFFFFF
                self.instructions.pop()
                self.patch(0, handler_addr)
            else:
                raise ValueError("set-irq-handler must follow a word reference")
            return i
        elif tok == "and":
            self._compile_binop(isa.AND)
            return i
        elif tok == "or":
            self._compile_binop(isa.OR)
            return i
        elif tok == "not":
            self.emit(isa.NOT)
            return i
        else:
            self.compile_tokens([tok])
            return i

    def _compile_type(self):
        str_ptr = self.PTR_TMP
        len_tmp = self.tmp_addr(self.stack_depth)
        idx_tmp = self.tmp_addr(self.stack_depth + 1)

        self.emit(isa.STORE, isa.MODE_DIRECT, str_ptr)
        self.emit(isa.LOAD, isa.MODE_INDIRECT, str_ptr)
        self.emit(isa.STORE, isa.MODE_DIRECT, len_tmp)
        self.emit(isa.LOAD, isa.MODE_IMM, 0)
        self.emit(isa.STORE, isa.MODE_DIRECT, idx_tmp)

        loop_start = len(self.instructions)

        self.emit(isa.LOAD, isa.MODE_DIRECT, idx_tmp)
        self.emit(isa.CMP, isa.MODE_DIRECT, len_tmp)
        exit_jmp = self.emit(isa.JNN, isa.MODE_DIRECT, 0)

        self.emit(isa.LOAD, isa.MODE_DIRECT, str_ptr)
        self.emit(isa.ADD, isa.MODE_DIRECT, idx_tmp)
        self.emit(isa.INC)
        self.emit(isa.STORE, isa.MODE_DIRECT, self.XTR_TMP)
        self.emit(isa.LOAD, isa.MODE_INDIRECT, self.XTR_TMP)
        self.emit(isa.OUT, isa.MODE_DIRECT, isa.PORT_STDOUT)

        self.emit(isa.LOAD, isa.MODE_DIRECT, idx_tmp)
        self.emit(isa.INC)
        self.emit(isa.STORE, isa.MODE_DIRECT, idx_tmp)

        self.emit(isa.JMP, isa.MODE_DIRECT, loop_start)

        exit_addr = len(self.instructions)
        self.patch(exit_jmp, exit_addr)

        self.vstack_pop()

    def _apply_patches(self):
        for addr, name in self.patches:
            if name in self.word_addrs:
                self.patch(addr, self.word_addrs[name])
            else:
                raise ValueError(f"Undefined word: '{name}'")

    def tokenize(self, source):
        tokens = []
        for line in source.splitlines():
            line = line.split("\\")[0]
            tokens.extend(line.split())
        return tokens

    def translate(self, src_path, bin_path):
        src_path = Path(src_path)
        bin_path = Path(bin_path)
        bin_path.parent.mkdir(parents=True, exist_ok=True)

        with open(src_path) as f:
            source = f.read()

        tokens = self.tokenize(source)

        irq_vector = self.emit(isa.JMP, isa.MODE_DIRECT, 0)

        main_start = len(self.instructions)
        self.patch(irq_vector, main_start)

        self.compile_tokens(tokens)
        self.emit(isa.HALT)

        self._apply_patches()

        write_binary(self.instructions, str(bin_path), self.data_section)
        self.dump_text(str(bin_path.with_suffix(".txt")))
        print(
            f"Translated: {len(self.instructions)} instructions, "
            f"{len(self.data_section)} data words -> {bin_path.name}"
        )


if __name__ == "__main__":
    import sys

    import config

    name = sys.argv[1] if len(sys.argv) > 1 else "sort"
    name = Path(name).stem

    src = config.EXAMPLES_DIR / f"{name}.forth"
    dest = config.BUILD_DIR / f"{name}.bin"

    t = Translator()
    t.translate(src, dest)