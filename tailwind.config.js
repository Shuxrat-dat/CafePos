/* Сборка стилей (нужен Node.js, один раз после изменения классов в шаблонах):
 *   npx tailwindcss@3.4.17 -c tailwind.config.js -i static/css/app.src.css -o static/css/app.css --minify
 * Раньше стили собирал в браузере файл tailwind.js на каждой странице; теперь браузер берёт готовый app.css. */
module.exports = {
  content: {
    files: [
      './templates/**/*.html',
      './{accounts,core,menu,pos,kitchen,reports,inventory,juma,printing,config}/**/*.{py,html}',
      './static/js/tour.js',
    ],
    // Скобки тегов Django ({% %}, {{ }}) склеивают соседний класс в один токен: заменяем скобки пробелом, текст внутри оставляем
    transform: {
      html: (code) => code.replace(/\{%|%\}|\{\{|\}\}/g, ' '),
    },
  },
  theme: {
    extend: {
      colors: {
        brand: { DEFAULT: '#7A3F1E', 50: '#FBF4EA', 100: '#F6E6D2', 200: '#EBCBA6', 600: '#6A3418', 700: '#572A13', 800: '#43200E' },
        caramel: { DEFAULT: '#C98B45', 100: '#F8EBD7' },
        sage: { DEFAULT: '#4E7B5C', 50: '#EEF5F0', 100: '#DCEBE0' },
        ink: '#1E1915', muted: '#7A6F63', line: '#E8DFD2', canvas: '#F7F3EC', night: '#1C1612',
      },
      fontFamily: { sans: ['Manrope', 'Segoe UI', 'system-ui', '-apple-system', 'Roboto', 'sans-serif'] },
      boxShadow: { card: '0 1px 2px rgba(60,36,18,.05), 0 4px 14px -6px rgba(60,36,18,.07)', pop: '0 18px 44px -14px rgba(60,36,18,.28)' },
    },
  },
};
