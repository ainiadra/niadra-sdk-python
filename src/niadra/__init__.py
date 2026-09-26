"""Niadra: the shared memory of every AI agent in a company.

```python
from niadra import Niadra, phone

niadra = Niadra(channel="whatsapp")
context = niadra.context(phone("+5511912345678"), conversation_id="thread-81")
```
"""

from niadra._async_client import AsyncNiadra
from niadra._client import Niadra
from niadra._version import __version__
from niadra.backing import BackingReport, UnbackedValue
from niadra.conversation import AsyncConversation, AsyncTask, Conversation, Task, current_session
from niadra.errors import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    ConflictError,
    NiadraError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
    UnprocessableEntityError,
    WrongCellError,
)
from niadra.handles import anonymous, app_user, email, phone, system_id, whatsapp, whatsapp_bsuid
from niadra.integrations.openai import wrap
from niadra.keys import ApiKey
from niadra.models import (
    BatchResponse,
    Closes,
    Content,
    Context,
    ContextStamp,
    EventItem,
    FeedbackAction,
    Handle,
    HistoryFilters,
    HistoryItem,
    MediaUpload,
    ModelUsage,
    ObjectRef,
    ObjectState,
    ObjectTimeline,
    OpenedItem,
    SearchResult,
    SpeakerRef,
    Subject,
    SubjectToken,
    TargetModel,
    TimelinePage,
)
from niadra.options import CacheOptions, QueueOptions, Timeouts
from niadra.tools import AsyncToolKit, ToolKit
from niadra.vocabulary import (
    AssertionMethod,
    DeliveryPath,
    EventKind,
    HandleType,
    Speaker,
    SubjectKind,
    Verification,
    Visibility,
)

__all__ = [
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "ApiKey",
    "AssertionMethod",
    "AsyncConversation",
    "AsyncNiadra",
    "AsyncTask",
    "AsyncToolKit",
    "AuthenticationError",
    "BackingReport",
    "BadRequestError",
    "BatchResponse",
    "CacheOptions",
    "Closes",
    "ConfigurationError",
    "ConflictError",
    "Content",
    "Context",
    "ContextStamp",
    "Conversation",
    "DeliveryPath",
    "EventItem",
    "EventKind",
    "FeedbackAction",
    "Handle",
    "HandleType",
    "HistoryFilters",
    "HistoryItem",
    "MediaUpload",
    "ModelUsage",
    "Niadra",
    "NiadraError",
    "NotFoundError",
    "ObjectRef",
    "ObjectState",
    "ObjectTimeline",
    "OpenedItem",
    "PermissionDeniedError",
    "QueueOptions",
    "RateLimitError",
    "SearchResult",
    "ServerError",
    "Speaker",
    "SpeakerRef",
    "Subject",
    "SubjectKind",
    "SubjectToken",
    "TargetModel",
    "Task",
    "TimelinePage",
    "Timeouts",
    "ToolKit",
    "UnbackedValue",
    "UnprocessableEntityError",
    "Verification",
    "Visibility",
    "WrongCellError",
    "__version__",
    "anonymous",
    "app_user",
    "current_session",
    "email",
    "phone",
    "system_id",
    "whatsapp",
    "whatsapp_bsuid",
    "wrap",
]
