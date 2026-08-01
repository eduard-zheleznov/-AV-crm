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


class InactiveListingError(AppError):
    """Avito explicitly reports that the listing is removed, closed or blocked."""


class PhoneButtonUnavailableError(AppError):
    """A loaded listing has no control that can reveal a phone number."""


class PhoneNotFoundError(AppError):
    """A phone control was present, but no valid phone number could be extracted."""


class BrowserOperationError(AppError):
    """A browser or page-loading operation failed for a technical reason."""


class ManualActionRequired(AppError):
    """The browser needs operator attention before automation may continue."""


class ManualReviewRequired(AppError):
    """A downstream CRM lead is unsafe to change without a human review."""


class OperatorStopRequested(AppError):
    """The operator stopped the current browser operation without failing its row."""


class NotificationError(AppError):
    """A notification provider rejected or failed to deliver a message."""


class CrmError(AppError):
    """LPTracker returned an error or an invalid response."""


class DuplicateContact(AppError):
    """The normalized phone is already present in the configured project."""
