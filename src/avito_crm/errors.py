class AppError(RuntimeError):
    """Expected operational error safe to show to an operator."""


class InstanceAlreadyRunning(AppError):
    """A mutually exclusive worker or controller is already active."""


class ConfigurationError(AppError):
    """Configuration is missing or inconsistent."""


class SourceError(AppError):
    """Queue source could not be read or updated."""


class InvalidListingError(AppError):
    """The queue item is not a supported Avito listing URL."""


class PhoneNotFoundError(AppError):
    """No valid phone number could be extracted from the listing."""


class ManualActionRequired(AppError):
    """The browser needs operator attention before automation may continue."""


class NotificationError(AppError):
    """A notification provider rejected or failed to deliver a message."""


class CrmError(AppError):
    """LPTracker returned an error or an invalid response."""


class DuplicateContact(AppError):
    """The normalized phone is already present in the configured project."""
