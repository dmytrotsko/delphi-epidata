from pathlib import Path
from typing import Dict, List, Set
from flask import Blueprint, render_template_string, request, make_response
from werkzeug.exceptions import Unauthorized, NotFound, BadRequest
from werkzeug.utils import redirect
from requests import post
from .._security import resolve_auth_token, APIUser, user_role_table
from .._config import ADMIN_PASSWORD, RECAPTCHA_SECRET_KEY, RECAPTCHA_SITE_KEY, REGISTER_WEBHOOK_TOKEN
from .._common import db

self_dir = Path(__file__).parent
# first argument is the endpoint name
bp = Blueprint("admin", __name__)

templates_dir = Path(__file__).parent / "templates"


def enable_admin() -> bool:
    return bool(ADMIN_PASSWORD)


def _require_admin():
    token = resolve_auth_token()
    if token is None or token != ADMIN_PASSWORD:
        raise Unauthorized()
    return token


def _parse_roles(roles: List[str]) -> Set[str]:
    return set(sorted(roles))


def _render(mode: str, token: str, flags: Dict, **kwargs):
    template = (templates_dir / "index.html").read_text("utf8")
    available_roles = db.execute(user_role_table.select()).fetchall()
    return render_template_string(
        template, mode=mode, token=token, flags=flags, roles=[r.name for r in available_roles], **kwargs
    )


@bp.route("/", methods=["GET", "POST"])
def _index():
    token = _require_admin()
    flags = dict()
    if request.method == "POST":
        # register a new user
        user = APIUser.insert_user(
            request.values["api_key"],
            _parse_roles(request.values.getlist("roles")),
            request.values.get("tracking") == "True",
            request.values.get("registered") == "True",
        )
        roles = _parse_roles(request.values.getlist("roles"))
        APIUser.assign_user_roles(user.id, roles)

        flags["banner"] = "Successfully Added"
    users = APIUser.list()
    return _render("overview", token, flags, users=users, user=dict())


@bp.route("/<int:user_id>", methods=["GET", "PUT", "POST", "DELETE"])
def _detail(user_id: int):
    token = _require_admin()
    user = APIUser.find_user(user_id=user_id)
    if not user:
        raise NotFound()
    if request.method == "DELETE" or "delete" in request.values:
        user.delete_user()
        return redirect(f"./?auth={token}")
    flags = dict()
    if request.method == "PUT" or request.method == "POST":
        user = user.update_user(
            request.values["api_key"],
            _parse_roles(request.values.getlist("roles")),
            request.values.get("tracking") == "True",
            request.values.get("registered") == "True",
        )
        flags['banner'] = 'Successfully Saved'
    return _render("detail", token, flags, user=user)


@bp.route("/register", methods=["POST"])
def _register():
    body = request.get_json()
    token = body.get("token")
    if token is None or token != REGISTER_WEBHOOK_TOKEN:
        raise Unauthorized()

    old_api_key = body["user_old_api_key"]
    db_user = APIUser.find_user(api_key=old_api_key)
    if db_user is None:
        raise BadRequest("invalid api key")
    new_api_key = body["user_new_api_key"]
    tracking = True if body["tracking"] == "Yes" else False
    db_user = db_user.update_user(new_api_key, db_user.roles, tracking, True)
    return make_response(f'Successfully registered the API key "{new_api_key}" and removed rate limit', 200)


def _verify_recaptcha():
    recaptcha_response = request.values["g-recaptcha-response"]
    url = "https://www.google.com/recaptcha/api/siteverify"
    # skip remote ip for now since behind proxy
    res = post(url, params=dict(secret=RECAPTCHA_SECRET_KEY, response=recaptcha_response)).json()
    if res["success"] is not True:
        raise BadRequest("invalid recaptcha key")


@bp.route("/create_key", methods=["GET", "POST"])
def _request_api_key():
    template = (templates_dir / "request.html").read_text("utf8")
    if request.method == "GET":
        return render_template_string(template, mode="request", recaptcha_key=RECAPTCHA_SITE_KEY)
    if request.method == "POST":
        if RECAPTCHA_SECRET_KEY:
            _verify_recaptcha()
        api_key = APIUser.register_new_key()
        return render_template_string(template, mode="result", api_key=api_key)
