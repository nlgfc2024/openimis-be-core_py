from django.http import Http404, StreamingHttpResponse, HttpResponse
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_protect
from django.contrib.auth import logout
from rest_framework.authentication import SessionAuthentication
from graphql_jwt.settings import jwt_settings
from graphql_jwt.utils import delete_cookie
from core.authentication import password_expired
from core.jwt_authentication import JWTAuthentication
from isodate import strftime
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import User, ExportableQueryModel
from .scheduler import scheduler
from .serializers import UserSerializer
from django.utils.translation import gettext as _


def check_user_rights(rights):
    class UserWithRights(IsAuthenticated):
        def has_permission(self, request, view):
            return super().has_permission(request, view) and request.user.has_perms(
                rights
            )

    return UserWithRights


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    # If we don't specify the IsAuthenticated, the framework will look for the core.user_view permission and prevent
    # any access from non-admin users
    permission_classes = [IsAuthenticated]

    @action(detail=False)
    def current_user(self, request):
        if password_expired(request.user):
            return Response({'detail': 'PASSWORD_EXPIRED'}, status=status.HTTP_401_UNAUTHORIZED)
        serializer = self.get_serializer(request.user, many=False)
        data = dict(serializer.data)
        authenticator = request.successful_authenticator
        data['authMode'] = (
            'jwt' if isinstance(authenticator, JWTAuthentication)
            else 'session' if isinstance(authenticator, SessionAuthentication)
            else 'other'
        )
        return Response(data)


@api_view(["GET"])
@require_GET
def fetch_export(request):
    requested_export = request.query_params.get("export")
    export = ExportableQueryModel.objects.filter(name=requested_export).first()
    if not export:
        raise Http404
    elif export.user != request.user:
        raise PermissionDenied(
            {"message": _("Only user requesting export can fetch request")}
        )
    elif export.is_deleted:
        return Response(
            data="Export csv file was removed from server.", status=status.HTTP_410_GONE
        )

    export_file_name = f"export_{export.model}_{strftime(export.create_date, '%d_%m_%Y')}.{export.file_format}"
    if export.file_format == ExportableQueryModel.FileFormat.CSV:
        response = StreamingHttpResponse(
            open(export.content.path, "rb"),
            content_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{export_file_name}"'
            },
        )
    elif ExportableQueryModel.FileFormat.XLSX:
        response = StreamingHttpResponse(
            open(export.content.path, "rb"),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="{export_file_name}"'
            },
        )
    else:
        return Response(
            data="Unsupported file format.", status=status.HTTP_400_BAD_REQUEST
        )

    return response


def _serialize_job(job):
    return "name: %s, trigger: %s, next run: %s, handler: %s" % (
        job.name,
        job.trigger,
        job.next_run_time,
        job.func,
    )


@api_view(["GET"])
@require_GET
def get_scheduled_jobs(request):
    return Response([_serialize_job(job) for job in scheduler.get_jobs()])


@require_POST
@csrf_protect
def logout_session(request):
    """End both Django and JWT authentication, even if the JWT has expired.

    This deliberately uses Django rather than DRF authentication: an invalid JWT
    must not prevent logout of a valid Django session. CSRF protection still runs.
    """
    logout(request)
    response = HttpResponse(status=204)
    delete_cookie(response, jwt_settings.JWT_COOKIE_NAME)
    delete_cookie(response, jwt_settings.JWT_REFRESH_TOKEN_COOKIE_NAME)
    return response
