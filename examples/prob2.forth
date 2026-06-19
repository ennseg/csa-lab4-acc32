\ разница квадрата суммы и суммы квадратов для 1..100
variable n
variable i
variable sum
variable sumsq
variable sq
variable total

variable num
variable digit
variable count
variable temp
variable bufptr
: extract-digits
  0 count !
  BEGIN
    num @ 10 / temp !
    num @ temp @ 10 * - digit !
    500 count @ + bufptr !
    digit @ bufptr @ !
    temp @ num !
    count @ 1 + count !
    num @ 0 =
  UNTIL ;
: print-digits
  BEGIN
    count @ 1 - count !
    500 count @ + bufptr !
    bufptr @ @ 48 +
    emit
    count @ 0 =
  UNTIL ;
: print-num extract-digits print-digits ;

100 n !
0 sum !
0 sumsq !
1 i !
BEGIN
  \ sum += i
  sum @ i @ + sum !
  \ sumsq += i*i
  i @ i @ * sq !
  sumsq @ sq @ + sumsq !
  i @ 1 + i !
  i @ n @ 1 + =
UNTIL
\ total = sum^2 - sumsq
sum @ sum @ * total !
total @ sumsq @ - total !
total @ num ! print-num
halt
