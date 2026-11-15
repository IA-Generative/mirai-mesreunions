"""Point d'entrée du QR Enrollment Portal (gunicorn ``app.main:application``).

Construit les dépendances depuis l'environnement puis délègue à
``app.portal.create_app``. Refuse de démarrer si la configuration ouvrirait
le service sans Passage2 (cf. ``libs.shared.app.passage2``).
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import UPLOAD_PORTAL_BASE_URL, load_ext_db  # noqa: E402
from libs.shared.app.database import create_session_factory  # noqa: E402
from libs.shared.app.keycloak_users import KeycloakUserResolver  # noqa: E402
from libs.shared.app.passage2 import Passage2Config  # noqa: E402
from libs.shared.app.security import require_strong_shared_secret  # noqa: E402

from app.portal import create_app, internal_device_api  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

passage2 = Passage2Config.from_env()
if not passage2.is_development:
    require_strong_shared_secret("INTERNAL_API_TOKEN")

application = create_app(
    passage2=passage2,
    resolve_user=KeycloakUserResolver.from_env().resolve_by_email,
    session_factory=create_session_factory(load_ext_db()),
    upload_base_url=os.getenv("QR_UPLOAD_BASE_URL", UPLOAD_PORTAL_BASE_URL),
    internal_api=internal_device_api,
)

if __name__ == "__main__":
    application.run(host="0.0.0.0", port=int(os.getenv("QR_PORTAL_PORT", "8093")),
                    debug=passage2.is_development)
