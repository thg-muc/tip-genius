/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./public/**/*.{html,js}'],
  theme: {
    extend: {
      colors: {
        dark: {
          DEFAULT: '#1a1a1a',
          card: '#2d2d2d'
        }
      }
    }
  },
  darkMode: 'media',
  plugins: []
}
