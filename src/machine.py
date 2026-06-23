from pathlib import Path

import config
import isa
from translator import read_binary


class DataPath:
    cu: "ControlUnit"

    def __init__(self, program):

        self.cu = None  # type: ignore[assignment]

        self.IP = config.START_IP
        self.IR = 0
        self.AR = 0
        self.DR = 0
        self.AC = 0
        self.AC_SHADOW = 0

        self.AC_AR = 0
        self.ACS_AR = 0
        self.AC_valid = 0
        self.ACS_valid = 0
        self.SR = {"N": 0, "Z": 0, "V": 0, "C": 0}
        self.SP = config.STACK_TOP

        self.instruction_memory, data_section = self.load_program(program)
        self.data_memory = [0] * config.DATA_MEMORY_SIZE

        for addr, value in data_section:
            self.data_memory[addr] = value

        self.output_buffer = []
        self.io_ports = {}

        self.last_alu_result = 0

    def load_program(self, program):
        if isinstance(program, list):
            return program, []
        elif isinstance(program, str) and program.endswith(".bin"):
            return read_binary(program)
        else:
            raise ValueError(f"Unknown program format: {program}")

    def alu(self, op, a, update_flags=True):
        b = self.DR

        carry_out = None

        if op == "ADD":
            raw = (a & 0xFFFFFFFF) + (b & 0xFFFFFFFF)
            carry_out = 1 if (raw & 0x100000000) else 0
            result = raw
        elif op == "ADC":
            raw = (a & 0xFFFFFFFF) + (b & 0xFFFFFFFF) + self.SR["C"]
            carry_out = 1 if (raw & 0x100000000) else 0
            result = raw
        elif op == "SUB":
            raw = (a & 0xFFFFFFFF) - (b & 0xFFFFFFFF)
            carry_out = 1 if raw < 0 else 0
            result = raw
        elif op == "INC":
            raw = (a & 0xFFFFFFFF) + 1
            carry_out = 1 if (raw & 0x100000000) else 0
            result = raw
        elif op == "DEC":
            raw = (a & 0xFFFFFFFF) - 1
            carry_out = 1 if raw < 0 else 0
            result = raw
        elif op == "MUL":
            result = a * b
        elif op == "DIV":
            result = a // b
        elif op == "AND":
            result = a & b
        elif op == "OR":
            result = a | b
        elif op == "NOT":
            result = ~a
        elif op == "PASS_AC":
            result = a
        elif op == "PASS_DR":
            result = b

        self.last_alu_result = result

        if update_flags:
            self.update_flags(result, carry_out)
        return result

    def update_flags(self, value, carry_out=None):
        value = value & 0xFFFFFFFF
        if value & 0x80000000:
            value -= 0x100000000

        self.cu.latch_SR("Z", 1 if value == 0 else 0)
        self.cu.latch_SR("N", 1 if value < 0 else 0)
        self.cu.latch_SR("V", 0)

        if carry_out is not None:
            self.cu.latch_SR("C", carry_out)


class ControlUnit:
    def __init__(self, datapath, input_interrupts):
        self.dp = datapath
        self.input_interrupts = input_interrupts

        self.tick = 0
        self.state = "FETCH"
        self.exec_step = 0
        self.IE = 1
        self.opcode = 0
        self.mode = 0
        self.operand = 0
        self.IRQ = 0
        self.running = True
        self.log = []
        self.cu_signal = ""

    def step(self):
        self.tick += 1
        self.cu_signal = ""
        self.check_irq()

        current_state = self.state

        if self.state == "FETCH":
            self.fetch()
            self.state = "DECODE"

        elif self.state == "DECODE":
            self.decode()
            self.state = "EXECUTE"

        elif self.state == "EXECUTE":
            done = self.execute()

            if done:
                self.latch_step(self.step_mux(0))
                self.state = "FETCH"

        self.write_log(current_state)

    def fetch(self):
        self.latch_IR(self.instruction_mem_read(self.dp.IP))

    def decode(self):
        self.opcode = (self.dp.IR >> 27) & 0x1F
        self.mode = (self.dp.IR >> 25) & 0x03
        self.operand = self.dp.IR & 0x1FFFFFF
        if self.operand & 0x1000000:
            self.operand -= 0x2000000

        self.latch_IP(self.ip_mux("IP_INC"))
        self.cu_signal += (
            f" decode: {isa.OPCODE_NAMES.get(self.opcode, '???')} "
            f"mode={isa.MODE_NAMES.get(self.mode, '???')} operand={self.operand}"
        )
        self.exec_plan = self.build_plan()
        self.latch_step(self.step_mux(0))

    def execute(self):
        step_ = self.exec_plan[self.exec_step]
        if step_ is not None:
            step_()

        self.latch_step(self.step_mux("STEP_INC"))

        return self.exec_step >= len(self.exec_plan)

    def build_plan(self):
        op = self.opcode
        mode = self.mode
        operand = self.operand

        MEM_READ_OPS = (isa.ADD, isa.ADC, isa.SUB, isa.MUL, isa.DIV, isa.AND, isa.OR, isa.CMP)
        if (
            config.SUPERSCALAR
            and op in MEM_READ_OPS
            and mode in (isa.MODE_DIRECT, isa.MODE_INDIRECT)
        ):
            flush_prefix = [
                lambda: self._irq_flush_ac_step1(),
                lambda: self._irq_flush_ac_step2(),
                lambda: self._irq_flush_shadow_step1(),
                lambda: self._irq_flush_shadow_step2(),
            ]
            return flush_prefix + self._build_plan_core(op, mode, operand)

        return self._build_plan_core(op, mode, operand)

    def _build_plan_core(self, op, mode, operand):
        if op == isa.LOAD:
            if mode == isa.MODE_IMM:
                return [
                    lambda: self.latch_DR(self.dr_mux("IR_OPERAND")),
                    lambda: self.latch_AC(self.dp.alu("PASS_DR", self.ac_mux("AC"))),
                ]

            if mode == isa.MODE_DIRECT:
                if not config.SUPERSCALAR:
                    return [
                        lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                        lambda: self.latch_DR(self.dr_mux("MEM")),
                        lambda: self.latch_AC(self.dp.alu("PASS_DR", self.ac_mux("AC"))),
                    ]
                x = operand
                if self.dp.AC_valid and x == self.dp.AC_AR:
                    return [lambda: self._ss_dead_load(x)]
                if self.dp.ACS_valid and x == self.dp.ACS_AR:
                    return [
                        lambda: self.ss_swap_t1(),
                        lambda: self.ss_swap_t2(),
                        lambda: self.ss_swap_t3(),
                    ]
                if self.dp.AC_valid and self.dp.ACS_valid:
                    return [
                        lambda: self.ss_flush_ac_t1(),
                        lambda: self.ss_flush_ac_t2(),
                        lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                        lambda: self.latch_DR(self.dr_mux("MEM")),
                        lambda: self._ss_load_set(x),
                    ]
                return [
                    lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self._ss_load_set(x),
                ]

            if mode == isa.MODE_INDIRECT:
                if not config.SUPERSCALAR:
                    return [
                        lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                        lambda: self.latch_DR(self.dr_mux("MEM")),
                        lambda: self.latch_AR(self.ar_mux("DR")),
                        lambda: self.latch_DR(self.dr_mux("MEM")),
                        lambda: self.latch_AC(self.dp.alu("PASS_DR", self.ac_mux("AC"))),
                    ]
                return [
                    lambda: self._irq_flush_ac_step1(),
                    lambda: self._irq_flush_ac_step2(),
                    lambda: self._irq_flush_shadow_step1(),
                    lambda: self._irq_flush_shadow_step2(),
                    lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self.latch_AR(self.ar_mux("DR")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self.latch_AC(self.dp.alu("PASS_DR", self.ac_mux("AC"))),
                ]

        if op == isa.STORE:
            if mode == isa.MODE_DIRECT:
                if not config.SUPERSCALAR:
                    return [
                        lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                        lambda: (
                            self.dp.alu("PASS_AC", self.ac_mux("AC"), False),
                            self.latch_DR(self.dr_mux("ALU")),
                        ),
                        lambda: self.data_mem_write(self.dp.AR, self.dp.DR),
                    ]
                x = operand
                return [
                    lambda: self._ss_store_mark(x),
                    lambda: self.ss_swap_t1(),
                    lambda: self.ss_swap_t2(),
                    lambda: self.ss_swap_t3(),
                ]

            if mode == isa.MODE_INDIRECT:
                if not config.SUPERSCALAR:
                    return [
                        lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                        lambda: self.latch_DR(self.dr_mux("MEM")),
                        lambda: self.latch_AR(self.ar_mux("DR")),
                        lambda: (
                            self.dp.alu("PASS_AC", self.ac_mux("AC"), False),
                            self.latch_DR(self.dr_mux("ALU")),
                        ),
                        lambda: self.data_mem_write(self.dp.AR, self.dp.DR),
                    ]
                return [
                    lambda: self._irq_flush_ac_step1(),
                    lambda: self._irq_flush_ac_step2(),
                    lambda: self._irq_flush_shadow_step1(),
                    lambda: self._irq_flush_shadow_step2(),
                    lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self.latch_AR(self.ar_mux("DR")),
                    lambda: (
                        self.dp.alu("PASS_AC", self.ac_mux("AC"), False),
                        self.latch_DR(self.dr_mux("ALU")),
                    ),
                    lambda: self.data_mem_write(self.dp.AR, self.dp.DR),
                ]

        if op == isa.ADD:
            if mode == isa.MODE_DIRECT:
                return [
                    lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self.latch_AC(self.dp.alu("ADD", self.ac_mux("AC"))),
                ]

            if mode == isa.MODE_IMM:
                return [
                    lambda: self.latch_DR(self.dr_mux("IR_OPERAND")),
                    lambda: self.latch_AC(self.dp.alu("ADD", self.ac_mux("AC"))),
                ]

        if op == isa.ADC:
            if mode == isa.MODE_DIRECT:
                return [
                    lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self.latch_AC(self.dp.alu("ADC", self.ac_mux("AC"))),
                ]

            if mode == isa.MODE_IMM:
                return [
                    lambda: self.latch_DR(self.dr_mux("IR_OPERAND")),
                    lambda: self.latch_AC(self.dp.alu("ADC", self.ac_mux("AC"))),
                ]

        if op == isa.SUB:
            if mode == isa.MODE_DIRECT:
                return [
                    lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self.latch_AC(self.dp.alu("SUB", self.ac_mux("AC"))),
                ]

            if mode == isa.MODE_IMM:
                return [
                    lambda: self.latch_DR(self.dr_mux("IR_OPERAND")),
                    lambda: self.latch_AC(self.dp.alu("SUB", self.ac_mux("AC"))),
                ]

        if op == isa.MUL:
            if mode == isa.MODE_DIRECT:
                return [
                    lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self.latch_AC(self.dp.alu("MUL", self.ac_mux("AC"))),
                ]

            if mode == isa.MODE_IMM:
                return [
                    lambda: self.latch_DR(self.dr_mux("IR_OPERAND")),
                    lambda: self.latch_AC(self.dp.alu("MUL", self.ac_mux("AC"))),
                ]

        if op == isa.DIV:
            if mode == isa.MODE_DIRECT:
                return [
                    lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self.latch_AC(self.dp.alu("DIV", self.ac_mux("AC"))),
                ]

            if mode == isa.MODE_IMM:
                return [
                    lambda: self.latch_DR(self.dr_mux("IR_OPERAND")),
                    lambda: self.latch_AC(self.dp.alu("DIV", self.ac_mux("AC"))),
                ]

        if op == isa.AND:
            if mode == isa.MODE_DIRECT:
                return [
                    lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self.latch_AC(self.dp.alu("AND", self.ac_mux("AC"))),
                ]

            if mode == isa.MODE_IMM:
                return [
                    lambda: self.latch_DR(self.dr_mux("IR_OPERAND")),
                    lambda: self.latch_AC(self.dp.alu("AND", self.ac_mux("AC"))),
                ]

        if op == isa.OR:
            if mode == isa.MODE_DIRECT:
                return [
                    lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self.latch_AC(self.dp.alu("OR", self.ac_mux("AC"))),
                ]

            if mode == isa.MODE_IMM:
                return [
                    lambda: self.latch_DR(self.dr_mux("IR_OPERAND")),
                    lambda: self.latch_AC(self.dp.alu("OR", self.ac_mux("AC"))),
                ]

        if op == isa.NOT:
            return [lambda: self.latch_AC(self.dp.alu("NOT", self.ac_mux("AC")))]

        if op == isa.INC:
            return [lambda: self.latch_AC(self.dp.alu("INC", self.ac_mux("AC")))]

        if op == isa.DEC:
            return [lambda: self.latch_AC(self.dp.alu("DEC", self.ac_mux("AC")))]

        if op == isa.CMP:
            if mode == isa.MODE_DIRECT:
                return [
                    lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self.dp.alu("SUB", self.ac_mux("AC")),
                ]

            if mode == isa.MODE_IMM:
                return [
                    lambda: self.latch_DR(self.dr_mux("IR_OPERAND")),
                    lambda: self.dp.alu("SUB", self.ac_mux("AC")),
                ]

        if op == isa.JMP:
            return [lambda: self.latch_IP(self.ip_mux("IR_OPERAND"))]

        if op == isa.JZ:
            return [lambda: self.latch_IP(self.ip_mux("IR_OPERAND")) if self.dp.SR["Z"] else None]

        if op == isa.JN:
            return [lambda: self.latch_IP(self.ip_mux("IR_OPERAND")) if self.dp.SR["N"] else None]

        if op == isa.JNZ:
            return [
                lambda: self.latch_IP(self.ip_mux("IR_OPERAND")) if not self.dp.SR["Z"] else None
            ]

        if op == isa.JNN:
            return [
                lambda: self.latch_IP(self.ip_mux("IR_OPERAND")) if not self.dp.SR["N"] else None
            ]

        if op == isa.PUSH:
            return [
                lambda: self.latch_AR(self.ar_mux("SP")),
                lambda: (
                    self.dp.alu("PASS_AC", self.ac_mux("AC"), False),
                    self.latch_DR(self.dr_mux("ALU")),
                ),
                lambda: self.data_mem_write(self.dp.AR, self.dp.DR),
                lambda: self.latch_SP(self.sp_mux("SP_DEC")),
            ]

        if op == isa.POP:
            return [
                lambda: self.latch_SP(self.sp_mux("SP_INC")),
                lambda: self.latch_AR(self.ar_mux("SP")),
                lambda: self.latch_DR(self.dr_mux("MEM")),
                lambda: self.latch_AC(self.dp.alu("PASS_DR", self.ac_mux("AC"))),
            ]

        if op == isa.CALL:
            if mode == isa.MODE_DIRECT:
                return [
                    lambda: self.latch_AR(self.ar_mux("SP")),
                    lambda: self.latch_DR(self.dr_mux("IP")),
                    lambda: self.data_mem_write(self.dp.AR, self.dp.DR),
                    lambda: self.latch_SP(self.sp_mux("SP_DEC")),
                    lambda: self.latch_IP(self.ip_mux("IR_OPERAND")),
                ]

            if mode == isa.MODE_INDIRECT:
                flush = []
                if config.SUPERSCALAR:
                    flush = [
                        lambda: self._irq_flush_ac_step1(),
                        lambda: self._irq_flush_ac_step2(),
                        lambda: self._irq_flush_shadow_step1(),
                        lambda: self._irq_flush_shadow_step2(),
                    ]
                return flush + [
                    lambda: self.latch_AR(self.ar_mux("SP")),
                    lambda: self.latch_DR(self.dr_mux("IP")),
                    lambda: self.data_mem_write(self.dp.AR, self.dp.DR),
                    lambda: self.latch_SP(self.sp_mux("SP_DEC")),
                    lambda: self.latch_AR(self.ar_mux("IR_OPERAND")),
                    lambda: self.latch_DR(self.dr_mux("MEM")),
                    lambda: self.latch_IP(self.ip_mux("DR")),
                ]

        if op == isa.RET:
            return [
                lambda: self.latch_SP(self.sp_mux("SP_INC")),
                lambda: self.latch_AR(self.ar_mux("SP")),
                lambda: self.latch_DR(self.dr_mux("MEM")),
                lambda: self.latch_IP(self.ip_mux("DR")),
            ]

        if op == isa.IN:
            return [
                lambda: self.latch_DR(self.dr_mux("IO")),
                lambda: self.latch_AC(self.dp.alu("PASS_DR", self.ac_mux("AC"))),
            ]

        if op == isa.OUT:
            return [
                lambda: self.io_write(
                    operand, self.dp.alu("PASS_AC", self.ac_mux("AC"), update_flags=False)
                ),
            ]

        if op == isa.IRET:
            return [
                lambda: self.latch_SP(self.sp_mux("SP_INC")),
                lambda: self.latch_AR(self.ar_mux("SP")),
                lambda: self.latch_DR(self.dr_mux("MEM")),
                lambda: self.unpack_SR(self.dp.DR),
                lambda: self.latch_SP(self.sp_mux("SP_INC")),
                lambda: self.latch_AR(self.ar_mux("SP")),
                lambda: self.latch_DR(self.dr_mux("MEM")),
                lambda: self.latch_AC(self.dp.alu("PASS_DR", self.ac_mux("AC"), False)),
                lambda: self.latch_SP(self.sp_mux("SP_INC")),
                lambda: self.latch_AR(self.ar_mux("SP")),
                lambda: self.latch_DR(self.dr_mux("MEM")),
                lambda: self.latch_IP(self.ip_mux("DR")),
                lambda: self.latch_IE(self.ie_mux(1)),
            ]

        if op == isa.HALT:
            if not config.SUPERSCALAR:
                return [lambda: self._do_halt("HALT")]
            return [
                lambda: self._irq_flush_ac_step1(),
                lambda: self._irq_flush_ac_step2(),
                lambda: self._irq_flush_shadow_step1(),
                lambda: self._irq_flush_shadow_step2(),
                lambda: self._do_halt(self.cu_signal + " HALT (parallel flush done)"),
            ]

        raise ValueError(f"Unknown opcode={op:#04x} mode={mode} operand={operand}")

    def run(self):
        while self.running:
            if config.MAX_TICKS is not None and self.tick >= config.MAX_TICKS:
                print(f"\n[остановка: достигнут лимит {config.MAX_TICKS} тактов]")
                break
            self.step()

        print("\n Output")
        print("".join(self.dp.output_buffer))
        print("\n Trassing")
        for entry in self.log:
            print(entry)
        print("\n Output")
        print("".join(self.dp.output_buffer))

    def latch_IR(self, value):
        self.dp.IR = value
        self.cu_signal += f"latch_IR <- {value} "

    def latch_IP(self, value):
        self.dp.IP = value
        self.cu_signal += f"latch_IP <- {value} "

    def latch_AR(self, value):
        self.dp.AR = value
        self.cu_signal += f"latch_AR <- {value} "

    def latch_DR(self, value):
        self.dp.DR = value
        self.cu_signal += f"latch_DR <- {value} "

    def latch_AC(self, value):
        value = value & 0xFFFFFFFF
        if value & 0x80000000:
            value -= 0x100000000
        self.dp.AC = value
        self.cu_signal += f"latch_AC <- {value} "

    def latch_AC_SHADOW(self, value):
        self.dp.AC_SHADOW = value
        self.cu_signal += f"latch_AC_SHADOW <- {value} "

    def latch_AC_AR(self, value):
        self.dp.AC_AR = value
        self.cu_signal += f"latch_AC_AR <- {value} "

    def latch_ACS_AR(self, value):
        self.dp.ACS_AR = value
        self.cu_signal += f"latch_ACS_AR <- {value} "

    def set_ac_valid(self, v):
        self.dp.AC_valid = v
        self.cu_signal += f"set_AC_valid <- {v} "

    def set_acs_valid(self, v):
        self.dp.ACS_valid = v
        self.cu_signal += f"set_ACS_valid <- {v} "

    def ss_swap_t1(self):
        self.latch_DR(self.dr_mux("AC"))
        self.latch_AR(self.ar_mux("ACS_AR"))

    def ss_swap_t2(self):
        old_ac_valid = self.dp.AC_valid
        old_acs_valid = self.dp.ACS_valid
        self.latch_AC(self.dp.alu("PASS_AC", self.ac_mux("AC_SHADOW")))
        self.latch_ACS_AR(self.ars_mux("AC_AR"))
        self.set_ac_valid(old_acs_valid)
        self.set_acs_valid(old_ac_valid)

    def ss_swap_t3(self):
        self.latch_AC_SHADOW(self.dp.DR)
        self.latch_AC_AR(self.ar_mux("AR"))
        self.cu_signal += (
            f"| SWAP done: ac/{self._slot(self.dp.AC_valid, self.dp.AC_AR)} "
            f"shadow/{self._slot(self.dp.ACS_valid, self.dp.ACS_AR)} "
        )

    def ss_flush_ac_t1(self):
        self.latch_AR(self.ar_mux("AC_AR"))
        self.dp.alu("PASS_AC", self.ac_mux("AC"), False)
        self.latch_DR(self.dr_mux("ALU"))

    def ss_flush_ac_t2(self):
        self.data_mem_write(self.dp.AR, self.dp.DR)
        self.set_ac_valid(self.acv_mux("invalid"))
        self.cu_signal += "| FLUSH ac done "

    def ss_flush_shadow_t1(self):
        self.latch_AR(self.ar_mux("ACS_AR"))
        self.dp.alu("PASS_AC", self.ac_mux("AC_SHADOW"), False)
        self.latch_DR(self.dr_mux("ALU"))

    def ss_flush_shadow_t2(self):
        self.data_mem_write(self.dp.AR, self.dp.DR)
        self.set_acs_valid(self.acsv_mux("invalid"))
        self.cu_signal += "| FLUSH shadow done "

    def _slot(self, valid, addr):
        return str(addr) if valid else "_"

    def _ss_store_mark(self, x):
        self.latch_AC_AR(x)
        self.set_ac_valid(self.acv_mux("valid"))
        self.cu_signal += f"STORE-MARK ac belongs [{x}] "

    def _ss_dead_load(self, x):
        self.cu_signal += f"DEAD-LOAD [{x}] (already in AC) "

    def _ss_load_set(self, x):
        self.latch_AC(self.dp.alu("PASS_DR", self.ac_mux("AC")))
        self.latch_AC_AR(x)
        self.set_ac_valid(self.acv_mux("valid"))

    def _do_halt(self, signal):
        self.running = False
        self.cu_signal = signal

    def latch_SP(self, value):
        self.dp.SP = value
        self.cu_signal += f"latch_SP <- {value} "

    def latch_SR(self, key, value):
        self.dp.SR[key] = value

    def ie_mux(self, sel):
        if sel == 0:
            return 0
        if sel == 1:
            return 1
        raise ValueError(f"Unknown IE_MUX sel: {sel}")

    def latch_IE(self, value):
        self.IE = value

    def step_mux(self, sel):
        if sel == 0:
            return 0
        if sel == 1:
            return 1
        if sel == "STEP_INC":
            return self.exec_step + 1
        raise ValueError(f"Unknown STEP_MUX sel: {sel}")

    def latch_step(self, value):
        self.exec_step = value

    def unpack_SR(self, value):
        self.IE = (value >> 4) & 1
        self.dp.SR["N"] = (value >> 3) & 1
        self.dp.SR["Z"] = (value >> 2) & 1
        self.dp.SR["V"] = (value >> 1) & 1
        self.dp.SR["C"] = value & 1

    def ip_mux(self, sel):
        if sel == "IP_INC":
            result = self.dp.IP + 1
        elif sel == "IR_OPERAND":
            result = self.operand
        elif sel == "DR":
            result = self.dp.DR
        elif sel == "VECTOR":
            result = self.instruction_mem_read(0) & 0x1FFFFFF
        else:
            raise ValueError(f"Unknown IP_MUX sel: {sel}")

        self.cu_signal += f" | IP_MUX({sel}) -> {result} "
        return result

    def ar_mux(self, sel):
        if sel == "SP":
            result = self.dp.SP
        elif sel == "IR_OPERAND":
            result = self.operand
        elif sel == "DR":
            result = self.dp.DR
        elif sel == "AC_AR":
            result = self.dp.AC_AR
        elif sel == "ACS_AR":
            result = self.dp.ACS_AR
        elif sel == "AR":
            result = self.dp.AR
        else:
            raise ValueError(f"Unknown AR_MUX sel: {sel}")

        self.cu_signal += f" | AR_MUX({sel}) -> {result} "
        return result

    def dr_mux(self, sel):
        if sel == "MEM":
            result = self.data_mem_read(self.dp.AR)
        elif sel == "IP":
            result = self.dp.IP
        elif sel == "IR_OPERAND":
            result = self.operand
        elif sel == "IO":
            result = self.io_read(self.operand)
        elif sel == "ALU":
            result = self.dp.last_alu_result
        elif sel == "AC":
            result = self.dp.AC
        elif sel == "SR":
            s = self.dp.SR
            result = (self.IE << 4) | (s["N"] << 3) | (s["Z"] << 2) | (s["V"] << 1) | s["C"]
        else:
            raise ValueError(f"Unknown DR_MUX sel: {sel}")

        self.cu_signal += f" | DR_MUX({sel}) -> {result} "
        return result

    def sp_mux(self, sel):
        if sel == "SP_DEC":
            result = self.dp.SP - 1
        elif sel == "SP_INC":
            result = self.dp.SP + 1
        else:
            raise ValueError(f"Unknown SP_MUX sel: {sel}")

        self.cu_signal += f" | SP_MUX({sel}) -> {result} "
        return result

    def ac_mux(self, sel):
        if sel == "AC":
            result = self.dp.AC
        elif sel == "AC_SHADOW":
            result = self.dp.AC_SHADOW
        else:
            raise ValueError(f"Unknown AC_MUX sel: {sel}")

        self.cu_signal += f" | AC_MUX({sel}) -> {result} "
        return result

    def ars_mux(self, sel):
        if sel == "AR":
            result = self.dp.AR
        elif sel == "AC_AR":
            result = self.dp.AC_AR
        else:
            raise ValueError(f"Unknown ARS_MUX sel: {sel}")

        self.cu_signal += f" | ARS_MUX({sel}) -> {result} "
        return result

    def acv_mux(self, sel):
        if sel == "valid":
            result = 1
        elif sel == "invalid":
            result = 0
        else:
            raise ValueError(f"Unknown ACV_MUX sel: {sel}")
        return result

    def acsv_mux(self, sel):
        if sel == "valid":
            result = 1
        elif sel == "invalid":
            result = 0
        else:
            raise ValueError(f"Unknown ACSV_MUX sel: {sel}")
        return result

    def cmp1(self):
        if self.dp.AR == self.dp.AC_AR:
            return 1
        else:
            return 0

    def cmp2(self):
        if self.dp.AR == self.dp.ACS_AR:
            return 1
        else:
            return 0

    def data_mem_read(self, address):
        value = self.dp.data_memory[address]
        self.cu_signal += f" | MEM_READ[{address}] = {value} "

        return value

    def data_mem_write(self, address, value):
        self.dp.data_memory[address] = value
        self.cu_signal += f" | MEM_WRITE[{address}] = {value} "

    def instruction_mem_read(self, address):
        value = self.dp.instruction_memory[address]
        self.cu_signal += f" | INSTRUCTION_MEM_READ[{address}] = {value:#010x} "

        return value

    def check_irq(self):
        for tick, char in self.input_interrupts:
            if tick == self.tick:
                self.dp.io_ports[isa.PORT_STDIN] = ord(char)
                self.IRQ = 1
                self.cu_signal += f" | IRQ_SET port[{isa.PORT_STDIN}]='{char}' "

        if self.IRQ and self.IE and self.state == "FETCH":
            self.latch_IE(self.ie_mux(0))
            self.IRQ = 0

            self.exec_plan = self.build_irq_plan()
            self.latch_step(self.step_mux(0))
            self.state = "EXECUTE"
            self.cu_signal += " | IRQ_ENTER: IE=0, IP saved, jump to vector "

    def build_irq_plan(self):
        flush = []
        if config.SUPERSCALAR:
            flush = [
                lambda: self._irq_flush_ac_step1(),
                lambda: self._irq_flush_ac_step2(),
                lambda: self._irq_flush_shadow_step1(),
                lambda: self._irq_flush_shadow_step2(),
            ]
        return flush + [
            lambda: self.latch_AR(self.ar_mux("SP")),
            lambda: self.latch_DR(self.dr_mux("IP")),
            lambda: self.data_mem_write(self.dp.AR, self.dp.DR),
            lambda: self.latch_SP(self.sp_mux("SP_DEC")),
            lambda: self.latch_AR(self.ar_mux("SP")),
            lambda: (
                self.dp.alu("PASS_AC", self.ac_mux("AC"), False),
                self.latch_DR(self.dr_mux("ALU")),
            ),
            lambda: self.data_mem_write(self.dp.AR, self.dp.DR),
            lambda: self.latch_SP(self.sp_mux("SP_DEC")),
            lambda: self.latch_AR(self.ar_mux("SP")),
            lambda: self.latch_DR(self.dr_mux("SR")),
            lambda: self.data_mem_write(self.dp.AR, self.dp.DR),
            lambda: self.latch_SP(self.sp_mux("SP_DEC")),
            lambda: self.latch_IP(self.ip_mux("VECTOR")),
        ]

    def _irq_flush_ac_step1(self):
        if self.dp.AC_valid:
            self.ss_flush_ac_t1()

    def _irq_flush_ac_step2(self):
        if self.dp.AC_valid:
            self.ss_flush_ac_t2()

    def _irq_flush_shadow_step1(self):
        if self.dp.ACS_valid:
            self.ss_flush_shadow_t1()

    def _irq_flush_shadow_step2(self):
        if self.dp.ACS_valid:
            self.ss_flush_shadow_t2()

    def io_read(self, port):
        value = self.dp.io_ports.get(port, 0)
        self.cu_signal += f" | IO_READ port[{port}] = {value} "

        return value

    def io_write(self, port, value):
        self.dp.io_ports[port] = value

        if port == isa.PORT_STDOUT:
            char = chr(value & 0xFF)
            self.dp.output_buffer.append(char)
            self.cu_signal += f" | IO_WRITE port[{port}] = '{char}' "
        else:
            self.cu_signal += f" | IO_WRITE port[{port}] = {value} "

    def write_log(self, state):
        irq_mark = " [IRQ]" if not self.IE else ""

        sig = self.cu_signal.strip().strip("|").strip()
        sig = sig.replace("\x00", "\\0")

        entry = (
            f"tick={self.tick:4d} | "
            f"{state:7s} | "
            f"signal: {sig:110s} | "
            f"IP={self.dp.IP:4d} | "
            f"IR={self.dp.IR:#010x} | "
            f"AC={self.dp.AC:10d} | "
            f"AR={self.dp.AR:4d} | "
            f"DR={self.dp.DR:10d} | "
            f"SP={self.dp.SP:4d} | "
            f"N={self.dp.SR['N']} Z={self.dp.SR['Z']} V={self.dp.SR['V']} "
            f"C={self.dp.SR['C']} IE={self.IE}"
            f"{irq_mark}"
        )
        self.log.append(entry)


if __name__ == "__main__":
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else "sort"
    name = Path(name).stem
    bin_path = config.BUILD_DIR / f"{name}.bin"

    input_str = sys.argv[2] if len(sys.argv) > 2 else ""
    interrupts = []

    if input_str:
        interrupts = [
            (config.START_TICK + i * config.TICK_INTERVAL, c) for i, c in enumerate(input_str)
        ]
        interrupts.append((config.START_TICK + len(input_str) * config.TICK_INTERVAL, "\x00"))

    dp = DataPath(str(bin_path))
    cu = ControlUnit(dp, interrupts)
    dp.cu = cu
    cu.run()