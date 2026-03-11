import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        gray: {
          50: "#0f0c09",
          100: "#1a1410",
          200: "#2d2520",
          300: "#4a3f35",
          400: "#6b5d4f",
          500: "#8c7a64",
          600: "#a89478",
          700: "#cbb89a",
          800: "#e0d1b8",
          900: "#f0e6d3",
          950: "#f7f0e3",
        },
        blue: {
          400: "#d4543d",
          500: "#c23b22",
          600: "#a02e18",
        },
        surface: {
          900: "#f0e6d3",
          800: "#e0d1b8",
          700: "#cbb89a",
          600: "#a89478",
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
