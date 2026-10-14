/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        primary: "#2563EB",
        success: "#16a34a",
        failure: "#dc2626",
      },
    },
  },
  plugins: [],
};
