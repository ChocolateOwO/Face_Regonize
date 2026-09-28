"""Per-run controls: authenticated defaults, admin-only validation; no DB writes."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from app.auth.deps import get_current_user
from app.models.models import User
from app.services.scan_settings import RANGES, ScanSettings

class ScanSettingsRoute(APIRoute):
    """Omit raw invalid inputs so nonfinite numbers still produce a valid 422 JSON."""
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def validated(request):
            try:
                return await handler(request)
            except RequestValidationError as error:
                raise HTTPException(422, detail=[
                    {key: item[key] for key in ("loc", "msg", "type")}
                    for item in error.errors()
                ]) from error
        return validated

router = APIRouter(prefix="/api/scan-settings", tags=["scan-settings"], route_class=ScanSettingsRoute)

@router.get("")
def defaults(user: User = Depends(get_current_user)):
    return {"defaults": ScanSettings().model_dump(), "ranges": RANGES,
            "can_configure": user.role == "admin"}

def require_admin(user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(403, "Only admins may configure scanning settings")
    return user

@router.post("/validate")
def validate(body: ScanSettings, user: User = Depends(require_admin)):
    return body.model_dump()
