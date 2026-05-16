// Onglet "Préparation de réunion". Module wrapper PR4 : ré-exporte les fonctions
// hébergées par frontend/legacy.js (qui les publie sur `window`
// pour préserver les onclick="" du template).
//
// Ce fichier documente le périmètre du tab et prépare la migration
// PR5 (event delegation → mount/unmount propres).
//
// IMPORTANT : importer 'frontend/legacy.js' avant ces wrappers.
// Le shell.js s'en charge dans le bon ordre.

export const loadBriefs = window.loadBriefs;
export const showBriefList = window.showBriefList;
export const showBriefDetail = window.showBriefDetail;
export const renderBriefBody = window.renderBriefBody;
export const loadBriefAudioFiles = window.loadBriefAudioFiles;
export const loadBriefSeries = window.loadBriefSeries;
export const detachAudioFromBrief = window.detachAudioFromBrief;
export const linkAudioToBriefPrompt = window.linkAudioToBriefPrompt;
export const renameBriefPrompt = window.renameBriefPrompt;
export const toggleAmendBrief = window.toggleAmendBrief;
export const fillAmendForm = window.fillAmendForm;
export const buildAmendBriefJson = window.buildAmendBriefJson;
export const saveAmendBrief = window.saveAmendBrief;
export const addAmendAgendaItem = window.addAmendAgendaItem;
export const addAmendParticipant = window.addAmendParticipant;
export const addAmendThread = window.addAmendThread;
export const addAmendOpeningQuestion = window.addAmendOpeningQuestion;
export const addAmendRisk = window.addAmendRisk;
export const addAmendChecklistItem = window.addAmendChecklistItem;
export const deleteBrief = window.deleteBrief;
export const restoreBrief = window.restoreBrief;
export const deleteBriefPermanently = window.deleteBriefPermanently;
export const renderOlderThan90dBanner = window.renderOlderThan90dBanner;
export const dismissOlderThan90dBanner = window.dismissOlderThan90dBanner;
export const trashAllOlderThan90d = window.trashAllOlderThan90d;
export const restoreAmendSectionsState = window.restoreAmendSectionsState;
