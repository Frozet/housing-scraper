export async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, options);
  let body;
  try { body = await response.json(); } catch { throw new Error('Сервер вернул некорректный ответ.'); }
  if (!response.ok) throw new Error(body.detail || 'Не удалось выполнить запрос.');
  return body;
}

export function filterParams(filters) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (Array.isArray(value)) value.forEach(item => params.append(key, item));
    else if (value !== '') params.append(key, value);
  }
  return params;
}
