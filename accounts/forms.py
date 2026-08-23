from django.contrib.auth.forms import PasswordChangeForm, PasswordResetForm, SetPasswordForm

INPUT_CLASSES = (
    "mt-1.5 block w-full rounded-xl border border-gray-300 bg-white px-3.5 py-2.5 text-sm "
    "text-gray-900 placeholder-gray-400 shadow-sm transition "
    "focus:outline-none focus:ring-4 focus:ring-blue-900/10 focus:border-blue-900"
)


class TailwindFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", INPUT_CLASSES)


class StyledSetPasswordForm(TailwindFormMixin, SetPasswordForm):
    pass


class StyledPasswordResetForm(TailwindFormMixin, PasswordResetForm):
    pass


class StyledPasswordChangeForm(TailwindFormMixin, PasswordChangeForm):
    pass
