\ демонстрация execution token (' и execute)
\ процедура выбирается во время выполнения
\ её адрес (xt) хранится как данные и вызывается косвенно

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

: double 2 * ;
: triple 3 * ;

variable op
variable flag

1 flag !

flag @ 0 = IF
  ' double op !
ELSE
  ' triple op !
THEN

7 op @ execute
num ! print-num
32 emit

' double op !
7 op @ execute
num ! print-num
halt
