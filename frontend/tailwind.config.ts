import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        gray: {
          50: "#f7f0e3",
          100: "#f0e6d3",
          200: "#e0d1b8",
          300: "#cbb89a",
          400: "#a89478",
          500: "#8c7a64",
          600: "#6b5d4f",
          700: "#4a3f35",
          800: "#2d2520",
          900: "#1a1410",
          950: "#0f0c09",
        },
        blue: {
          400: "#d4543d",
          500: "#c23b22",
          600: "#a02e18",
        },
        surface: {
          900: "#1a1410",
          800: "#2d2520",
          700: "#4a3f35",
          600: "#6b5d4f",
        },
        accent: {
          500: "#c23b22",
          600: "#a02e18",
          400: "#d4543d",
        },
      },
      spacing: {
        safe: "env(safe-area-inset-bottom)",
        "safe-top": "env(safe-area-inset-top)",
        "safe-left": "env(safe-area-inset-left)",
        "safe-right": "env(safe-area-inset-right)",
      },
      minHeight: {
        touch: "44px",
      },
      minWidth: {
        touch: "44px",
      },
    },
  },
  plugins: [],
} satisfies Config;
