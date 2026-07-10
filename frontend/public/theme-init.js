"use strict";

if (localStorage.getItem("theme") === "dark") {
  document.documentElement.classList.add("dark");
  const themeColor = document.querySelector('meta[name="theme-color"]');
  if (!(themeColor instanceof HTMLMetaElement)) {
    throw new Error("theme-color meta element is required");
  }
  themeColor.content = "#1a1410";
}
