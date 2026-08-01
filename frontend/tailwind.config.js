/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        primary: {
          50: "#ECEFF9",
          100: "#BDC7EB",
          200: "#8F9FDE",
          300: "#677AD2",
          400: "#404DC5",
          500: "#262E85",
        },
        secondary: {
          50: "#E4F3FC",
          100: "#98D1F6",
          200: "#44AEE8",
          300: "#358BBA",
          400: "#246689",
          500: "#144058",
        },
        accent: {
          50: "#F5E5EB",
          100: "#E5B3C6",
          200: "#D67DA3",
          300: "#B55982",
          400: "#843F5E",
          500: "#53253A",
        },
        neutral: {
          // NOTE: FRONTEND_DESIGN_GUIDE.md has "#EDEFFO" for neutral-50 --
          // "O" isn't a valid hex digit. Read as #EDEFF0 (a neutral gray
          // endpoint, consistent with the rest of this scale trending gray
          // rather than blue-tinted like primary/secondary-50). Flag if the
          // original design intended something else.
          50: "#EDEFF0",
          100: "#C2C9CC",
          200: "#9BA3A8",
          300: "#7B8285",
          400: "#595F61",
          500: "#383B3D",
        },
        risk: {
          clear: "#10B981",
          caution: "#F59E0B",
          warning: "#D67DA3",
          critical: "#EF4444",
        },
      },
      fontSize: {
        xs: "0.75rem",
        sm: "0.875rem",
        base: "1rem",
        lg: "1.125rem",
        xl: "1.25rem",
        "2xl": "1.5rem",
        "3xl": "1.875rem",
        "4xl": "2.25rem",
      },
    },
  },
  plugins: [],
}
