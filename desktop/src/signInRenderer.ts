import "./desktopApi.js";

function requiredElement<ElementType extends HTMLElement>(
  selector: string,
  expectedType: new () => ElementType,
): ElementType {
  const element = document.querySelector(selector);
  if (!(element instanceof expectedType)) {
    throw new Error(`Required sign-in element is missing: ${selector}`);
  }
  return element;
}

window.addEventListener("DOMContentLoaded", () => {
  const signInButton = requiredElement("[data-sign-in]", HTMLButtonElement);
  const status = requiredElement("[data-status]", HTMLParagraphElement);

  signInButton.addEventListener("click", async () => {
    signInButton.disabled = true;
    signInButton.textContent = "Opening browser...";
    status.textContent = "Complete sign-in in your browser. This window will update automatically.";
    status.dataset.state = "working";
    try {
      await window.yinshiDesktop.signIn();
    } catch {
      signInButton.disabled = false;
      signInButton.textContent = "Sign in with Yinshi";
      status.textContent = "Sign-in did not complete. Check your connection and try again.";
      status.dataset.state = "error";
    }
  });
});
