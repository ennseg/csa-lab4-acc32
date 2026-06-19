\ сортировка массива пузырьком
\ массив в pstr

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

variable n
variable i
variable j
variable aj
variable aj1
variable vj
variable vj1
variable tmp2

5 n !
5 301 !
3 302 !
8 303 !
1 304 !
9 305 !

0 i !
BEGIN
  0 j !

  BEGIN
    301 j @ + aj !
    302 j @ + aj1 !

    aj @ @ vj !
    aj1 @ @ vj1 !

    vj @ vj1 @ > IF
      vj @ tmp2 !
      vj1 @ aj @ !
      tmp2 @ aj1 @ !

    THEN
    j @ 1 + j !
    j @ n @ 1 - =

  UNTIL
  i @ 1 + i !
  i @ n @ 1 - =

UNTIL

0 j !
BEGIN
  301 j @ + aj !
  aj @ @ num ! print-num
  32 emit
  j @ 1 + j !
  j @ n @ =
UNTIL
halt
