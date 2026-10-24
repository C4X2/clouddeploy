import requests


class CloudflareDeployedUrlNotSetException(Exception):
    pass


class CloudflareDeploymentFailureException(Exception):
    pass


def test_url_endpoint(url: str):
    if not url:
        raise CloudflareDeployedUrlNotSetException("The CLOUDFLARE_DEPLOYED_URL environment variable is not set and the deployed url is not set")

    res = requests.get(url)
    if res.status_code != 200:
        raise CloudflareDeploymentFailureException("The url specified for cloudflare is unreachable")