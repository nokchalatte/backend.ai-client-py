from . import exceptions
from . import session

__all__ = (
    *exceptions.__all__,
    *session.__all__,
)

__version__ = '21.09.0a1'


def get_user_agent():
    return 'Backend.AI Client for Python {0}'.format(__version__)
