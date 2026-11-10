"""Local client for the owner's hosted collector. No direct Vinted fallback."""
import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request


class ProviderError(Exception):
    def __init__(self, message, blocked=False, retry=0):
        super().__init__(message)
        self.blocked, self.retry = blocked, retry


class Connection:
    def __init__(self, path):
        self.path, self.lock = path, threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.exists(): return {}
            try: return json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError): return {}

    def public(self):
        data = self.read()
        return {"url": data.get("url", ""), "configured": bool(data.get("url") and data.get("api_key"))}

    def save(self, url, key):
        url, key = str(url).strip().rstrip("/"), str(key).strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev", parsed.netloc) or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("Adresse attendue : https://nom.sous-domaine.workers.dev")
        with self.lock:
            old = self.read()
            if not key and old.get("url") == url: key = old.get("api_key", "")
            if not re.fullmatch(r"rp_[A-Za-z0-9_-]{40,100}", key):
                raise ValueError("Colle la clé personnelle rp_… fournie par le propriétaire du service.")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + "." + secrets.token_hex(8))
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump({"url": url, "api_key": key}, output)
                os.replace(str(tmp), str(self.path))
            finally:
                if tmp.exists(): tmp.unlink()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class RemoteProvider:
    def __init__(self, connection=None):
        self.connection = connection
        self.opener = urllib.request.build_opener(NoRedirect())
        self.last_http = None

    def request(self, path, body=None):
        settings = self.connection.read() if self.connection else {}
        if not settings.get("url") or not settings.get("api_key"):
            raise ProviderError("Configure l'adresse du service et ta clé dans Données & connexion.", blocked=True)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(settings["url"] + path, data=data,
            headers={"Authorization": "Bearer " + settings["api_key"], "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=50) as response:
                self.last_http = response.status
                raw = response.read(4_000_001)
                if len(raw) > 4_000_000: raise ProviderError("Réponse du service trop volumineuse.", blocked=True)
                payload = json.loads(raw)
        except urllib.error.HTTPError as error:
            self.last_http = error.code
            if error.code in [401, 403]: raise ProviderError("Accès au service refusé : clé inconnue, expirée ou révoquée. Contacte son propriétaire.", blocked=True)
            try: detail = json.loads(error.read(4096)).get("error", "")
            except (ValueError, AttributeError): detail = ""
            if error.code == 429:
                try: retry = min(86400, max(5, int(error.headers.get("Retry-After", "300"))))
                except ValueError: retry = 300
                raise ProviderError("Limite du service atteinte. " + str(detail)[:180], retry=retry)
            if error.code in [400, 404, 409, 422, 503]:
                raise ProviderError("Service indisponible : " + (str(detail)[:250] or "configuration à vérifier"), blocked=True)
            raise ProviderError("Erreur du service distant (HTTP %s)." % error.code)
        except urllib.error.URLError:
            raise ProviderError("Service distant injoignable. Vérifie la connexion Internet et l'adresse configurée.")
        except (ValueError, OSError):
            raise ProviderError("Réponse du service invalide ou délai dépassé.")
        if not isinstance(payload, dict): raise ProviderError("Format du service invalide.", blocked=True)
        return payload

    def verify(self):
        result = self.request("/v1/access")
        if result.get("active") is not True: raise ProviderError("Accès non confirmé.", blocked=True)
        return result

    def search(self, query, profile, page=1, per_page=48):
        filters = {k: profile.get(k, []) for k in ["catalog_ids", "brand_ids", "size_ids", "status_ids"]}
        payload = self.request("/v1/search", {"query": query, "page": page, "per_page": per_page,
            "min_price": profile["min_price"], "max_price": profile["max_price"], **filters})
        if not isinstance(payload.get("items"), list): raise ProviderError("Liste d'annonces absente du service.", blocked=True)
        return payload["items"]
