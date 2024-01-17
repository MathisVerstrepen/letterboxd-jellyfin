# pylint: disable=missing-module-docstring
class RequestException(Exception):
    """
    Exception raised when a request to the Letterboxd fails
    """

    def __init__(self, message):
        self.message = message
        super().__init__(self.message)

class RadarrException(Exception):
    """
    Exception raised when a request to the Radarr fails
    """

    def __init__(self, message):
        self.message = message
        super().__init__(self.message)
        
class JellyfinException(Exception):
    """
    Exception raised when a request to the Radarr fails
    """

    def __init__(self, message):
        self.message = message
        super().__init__(self.message)