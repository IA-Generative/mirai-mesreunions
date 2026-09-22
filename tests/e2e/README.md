# Tests de bout en bout

Ici, le code de production parle à de **vrais serveurs HTTP locaux** qui jouent
le rôle des services voisins (hub LiteLLM, passerelle Kevent, video-ingest,
device-token-authority). Rien n'est remplacé dans le code testé : on remplace
l'autre bout du câble (`_fakes.py`). Ils tournent sans Docker ni base.

| Fichier | Incident reproduit | Ce qui est figé |
|---|---|---|
| `test_e2e_llm_chain_model_fallback.py` | 2026-09-16 : le hub répond `400 Invalid model name` pour `mistral-small-24b` et `chat-small`, quatre étapes LLM sur cinq sortent vides | la chaîne se replie sur `LLM_MODEL_FALLBACKS`, un seul détour 400 par nom, et le témoin sans repli reproduit l'incident |
| `test_e2e_kevent_gateway_token_inactive.py` | 2026-09-16 : `GET /jobs → 401 token inactive` — la même clé dépose les transcriptions | `KeventAuthError` sans rejeu, `/api/v1/queue-status` répond 503 `stale` et le cache protège la passerelle |
| `test_e2e_youtube_import_verdict.py` | placeholders YouTube « en cours » depuis 40 jours alors que le job video-ingest était `failed` | la liste lit le verdict du job : `failed` avec la cause en français, `retrying` avec l'heure, `running` jamais « en retard », verdict terminal mémorisé, video-ingest à terre = dégradé sans casser |

Exécution : `python -m pytest tests/e2e/` (inclus dans `tests/run-regression-campaign.sh`, Run 0, bloquant).

Les scénarios contre la pile Docker Compose complète restent dans
`tests/regression/` (ils se sautent d'eux-mêmes sans la pile).
