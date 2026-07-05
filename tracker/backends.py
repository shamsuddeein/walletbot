from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

class EmailOrUsernameBackend(ModelBackend):
    """
    Custom authentication backend that allows logging in using either
    an email address or a standard username.
    """
    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        if not username:
            return None

        # If username contains '@', look up by email; otherwise by username
        if "@" in username:
            lookup_kwargs = {"email__iexact": username}
        else:
            lookup_kwargs = {"username__iexact": username}

        try:
            user = UserModel.objects.get(**lookup_kwargs)
        except UserModel.DoesNotExist:
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
