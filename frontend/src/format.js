const integerFormat = new Intl.NumberFormat('ru-RU');
const decimalFormat = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
const dateFormat = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Asia/Irkutsk', dateStyle: 'short', timeStyle: 'short',
});

export const number = value => decimalFormat.format(value);
export const money = value => value == null ? '—' : `${integerFormat.format(value)} ₽`;
export const unitMoney = value => value == null ? '—' : `${number(value)} ₽`;
export const formatDate = value => value ? dateFormat.format(new Date(value)) : '—';
