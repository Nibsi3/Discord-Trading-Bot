import re
from openid.consumer.consumer import Consumer


class SteamOpenID:
    """
    Main library class that handle Steam OpenID authentication.
    """
    def __init__(self, realm: str, return_to: str):
        """
        Initializes SteamOpenID with realm and return_to URLs.

        :param realm: The URL of the realm that is requesting authentication.
        :type realm: str
        :param return_to: The URL that Steam should redirect to after authentication.
        :type return_to: str
        """
        self.realm = realm
        self.return_to = return_to

        self._provider = "https://steamcommunity.com/openid"

    class SteamIDExtractionError(Exception):
        """
        Custom exception for errors while extracting the Steam ID from the OpenID response.
        """
        def __init__(self, message="There was an error extracting the Steam ID from the identity url provided"):
            self.message = message
            super().__init__(self.message)

    def get_redirect_url(self) -> str:
        """
        Generates the redirect URL for initiating OpenID authentication.

        :return: The URL to redirect the user to for OpenID authentication.
        :rtype: str
        """
        consumer = Consumer({}, None)
        auth_begin = consumer.begin(self._provider)
        redirect_url = auth_begin.redirectURL(self.realm, self.return_to)
        return redirect_url

    def validate_results(self, query_string: dict):
        """
        Validates the OpenID response and extracts the Steam ID from the identity URL.

        :param query_string: The query string parameters from the OpenID response.
        :type query_string: dict
        :return: The extracted Steam ID if the verification is successful, otherwise it returns None.
        :rtype: str
        :raises SteamIDExtractionError: If there was an error extracting the Steam ID.
        """
        consumer = Consumer({}, None)
        response = consumer.complete(query_string, self.return_to)
        if response.status != "success":
            return None

        identity_url = response.identity_url
        pattern = r"https://steamcommunity\.com/openid/id/(?P<steam_id>\d+)"
        match = re.search(pattern, identity_url)
        if match:
            steam_id = match.group("steam_id")
            return steam_id
        else:
            raise self.SteamIDExtractionError()


