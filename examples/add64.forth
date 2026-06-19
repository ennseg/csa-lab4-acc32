\ сложение двух 64-битных чисел (двойная точность)
\ 64-битное число хранится в двух 32-битных половинах: hi и lo
\ перенос из младшей половины реализован командой +c (ADC)

variable a_hi
variable a_lo
variable b_hi
variable b_lo
variable r_hi
variable r_lo

variable num
variable digit
variable temp
variable count
variable bufptr

: extract-digits
  0 count !
  BEGIN
    num @ 10 / temp !
    num @ temp @ 10 * - digit !
    100 count @ + bufptr !
    digit @ bufptr @ !
    temp @ num !
    count @ 1 + count !
    num @ 0 =
  UNTIL ;

: print-digits
  BEGIN
    count @ 1 - count !
    100 count @ + bufptr !
    bufptr @ @ 48 +
    emit
    count @ 0 =
  UNTIL ;

: print-num extract-digits print-digits ;

3000000000 constant A_LOW
2000000000 constant B_LOW

0      a_hi !
A_LOW  a_lo !
0      b_hi !
B_LOW  b_lo !

a_lo @ b_lo @ + r_lo !

a_hi @ b_hi @ +c r_hi !

r_hi @ num ! print-num
32 emit
r_lo @ num ! print-num
halt
