// Onglet "Mes appareils" + "Enrôler un nouvel appareil". Module wrapper PR4 : ré-exporte les fonctions
// hébergées par frontend/legacy.js (qui les publie sur `window`
// pour préserver les onclick="" du template).
//
// Ce fichier documente le périmètre du tab et prépare la migration
// PR5 (event delegation → mount/unmount propres).
//
// IMPORTANT : importer 'frontend/legacy.js' avant ces wrappers.
// Le shell.js s'en charge dans le bon ordre.

export const generateCode = window.generateCode;
export const resetForm = window.resetForm;
export const loadDevices = window.loadDevices;
export const renameDevice = window.renameDevice;
export const revokeDevice = window.revokeDevice;
export const deleteDevicePermanently = window.deleteDevicePermanently;
export const revokeAllDevices = window.revokeAllDevices;
export const renewTokenByQr = window.renewTokenByQr;
export const toggleDeviceScope = window.toggleDeviceScope;
export const updateDeviceFilterButton = window.updateDeviceFilterButton;
export const showEnrollmentForm = window.showEnrollmentForm;
