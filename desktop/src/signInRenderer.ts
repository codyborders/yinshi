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
  const profileList = requiredElement("[data-profile-list]", HTMLDivElement);
  const profileButtons = requiredElement("[data-profile-buttons]", HTMLDivElement);

  void window.yinshiDesktop
    .listProfiles()
    .then((profiles) => {
      const availableProfiles = profiles.filter((profile) => profile.hasCredentials);
      if (availableProfiles.length === 0) return;
      for (const profile of availableProfiles) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "profile-button";
        button.textContent = profile.user.email;
        button.addEventListener("click", async () => {
          button.disabled = true;
          status.textContent = "Opening local profile...";
          status.dataset.state = "working";
          try {
            await window.yinshiDesktop.switchProfile(profile.user.id);
          } catch {
            button.disabled = false;
            status.textContent = "This profile could not be opened. Sign in again to refresh it.";
            status.dataset.state = "error";
          }
        });
        profileButtons.append(button);
      }
      profileList.hidden = false;
    })
    .catch(() => undefined);

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
