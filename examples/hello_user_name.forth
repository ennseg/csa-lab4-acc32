\ спрашивает имя, читает через прерывания, печатает приветствие
." What is your name? "
variable bufpos
variable bufaddr
variable ch
: store-char
  300 bufpos @ + bufaddr !
  ch @ bufaddr @ !
  bufpos @ 1 + bufpos !
;
variable i2
: say-hello
  72 emit 101 emit 108 emit 108 emit 111 emit 44 emit 32 emit
  0 i2 !
  BEGIN
    300 i2 @ + bufaddr !
    bufaddr @ @ emit
    i2 @ 1 + i2 !
    i2 @ bufpos @ =
  UNTIL
  33 emit
  halt
;
: irq-handler
  key ch !
  ch @ 0 = IF say-hello THEN
  ch @ store-char
  iret
;
irq-handler set-irq-handler
0 bufpos !
BEGIN 0 UNTIL
