// Sources renvoyées par OpenRAG : 2.2.1 (métadonnées à plat) et 2.3.1
// (métadonnées sous `chunk`). Le serveur aplatit déjà (app/modules/rag/
// _openrag.py) ; le widget le refait à l'entrée, par prudence.
// Fonction pure, testée par tests/unit/test_sources_openrag.js (node:test).

// Source OpenRAG 2.3 (métadonnées sous `chunk`) → forme à plat de 2.2.1 ; inchangée sinon.
export const aplatirSource = (s) => {
  if (!s || typeof s.chunk !== 'object' || s.chunk === null) return s;
  const { chunk, ...haut } = s;
  return { ...chunk, ...haut };
};
