\ вывод введённых символов (ввод через прерывания по расписанию)
variable ch
: irq-handler
  key ch !
  ch @ emit
  iret
;
irq-handler set-irq-handler
BEGIN 0 UNTIL
