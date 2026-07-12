import { contextBridge, ipcRenderer } from "electron";

import {
  DESKTOP_IPC_CHANNELS,
  type HostedApiRequest,
  type YinshiDesktopApi,
} from "./desktopApi.js";

const desktopApi: YinshiDesktopApi = Object.freeze({
  signIn: async (): Promise<void> => {
    await ipcRenderer.invoke(DESKTOP_IPC_CHANNELS.signIn);
  },
  signOut: async (): Promise<void> => {
    await ipcRenderer.invoke(DESKTOP_IPC_CHANNELS.signOut);
  },
  importLocalRepository: async () =>
    ipcRenderer.invoke(DESKTOP_IPC_CHANNELS.importLocalRepository),
  hostedRequest: async (request: HostedApiRequest) =>
    ipcRenderer.invoke(DESKTOP_IPC_CHANNELS.hostedRequest, request),
  fileVaultStatus: async () =>
    ipcRenderer.invoke(DESKTOP_IPC_CHANNELS.fileVaultStatus),
  listProfiles: async () =>
    ipcRenderer.invoke(DESKTOP_IPC_CHANNELS.listProfiles),
  switchProfile: async (userId: string) =>
    ipcRenderer.invoke(DESKTOP_IPC_CHANNELS.switchProfile, userId),
  removeProfile: async (userId: string) =>
    ipcRenderer.invoke(DESKTOP_IPC_CHANNELS.removeProfile, userId),
});

contextBridge.exposeInMainWorld("yinshiDesktop", desktopApi);
