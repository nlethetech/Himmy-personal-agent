/** @type {import('tailwindcss').Config} */
// Daybook — native macOS design language. White-with-opacity text tiers (the macOS label
// system), translucent fills over window vibrancy, a single quiet system-blue accent, SF Pro.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        mac: {
          // text — macOS label opacity tiers
          ink: "rgba(255,255,255,0.92)",
          ink2: "rgba(255,255,255,0.56)",
          ink3: "rgba(255,255,255,0.34)",
          ink4: "rgba(255,255,255,0.20)",
          // fills & separators (sit over the vibrancy)
          fill: "rgba(255,255,255,0.05)",
          fillHi: "rgba(255,255,255,0.08)",
          stroke: "rgba(255,255,255,0.08)",
          strokeHi: "rgba(255,255,255,0.14)",
          // accent — system blue
          accent: "#0A84FF",
          accentHi: "#409CFF",
          accentDim: "rgba(10,132,255,0.16)",
          // system status colors
          green: "#30D158",
          red: "#FF453A",
          orange: "#FF9F0A",
        },
      },
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', '"SF Pro Text"', 'system-ui', 'sans-serif'],
        display: ['"SF Pro Display"', '-apple-system', 'BlinkMacSystemFont', 'system-ui', 'sans-serif'],
      },
      borderRadius: {
        mac: "10px",
      },
      boxShadow: {
        mac: "0 1px 2px rgba(0,0,0,0.28), 0 10px 34px -16px rgba(0,0,0,0.55)",
        pop: "0 14px 48px -10px rgba(0,0,0,0.6), 0 0 0 0.5px rgba(255,255,255,0.06)",
        tab: "0 1px 2px rgba(0,0,0,0.25)",
      },
    },
  },
  plugins: [],
};
