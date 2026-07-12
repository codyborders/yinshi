const { notarize } = require("@electron/notarize");

module.exports = async function notarizeMacApplication(context) {
  if (process.env.YINSHI_RELEASE_BUILD !== "1" || context.electronPlatformName !== "darwin") {
    return;
  }
  const appleApiKey = process.env.APPLE_API_KEY;
  const appleApiKeyId = process.env.APPLE_API_KEY_ID;
  const appleApiIssuer = process.env.APPLE_API_ISSUER;
  if (!appleApiKey || !appleApiKeyId || !appleApiIssuer) {
    throw new Error("Release notarization credentials are required");
  }
  const applicationPath = `${context.appOutDir}/${context.packager.appInfo.productFilename}.app`;
  await notarize({
    appBundleId: context.packager.appInfo.id,
    appPath: applicationPath,
    appleApiKey,
    appleApiKeyId,
    appleApiIssuer,
  });
};
