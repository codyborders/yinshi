import { contextBridge, ipcRenderer } from "electron";

import { DESKTOP_IPC_CHANNELS, type YinshiDesktopApi } from "./desktopApi.js";

const desktopApi: YinshiDesktopApi = Object.freeze({
  signIn: async (): Promise<void> => {
    await ipcRenderer.invoke(DESKTOP_IPC_CHANNELS.signIn);
  },
  signOut: async (): Promise<void> => {
    await ipcRenderer.invoke(DESKTOP_IPC_CHANNELS.signOut);
  },
  importLocalRepository: async () =>
    ipcRenderer.invoke(DESKTOP_IPC_CHANNELS.importLocalRepository),
});

contextBridge.exposeInMainWorld("yinshiDesktop", desktopApi);
