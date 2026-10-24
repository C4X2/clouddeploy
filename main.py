import os
import time

from argparse import Namespace

import argparse
from cloudflare.types.workers.scripts import version_create_params

import terminal_printer as exp
from dotenv import load_dotenv
from cloudflare import Cloudflare
import json
import datetime
import git_manager as gm
from image_optimizer import optimize_images
from pre_deploy_hooks import (
    generate_sitemap,
    inject_lazy_loading_in_html,
    prune_unused_assets,
    validate_asset_limits_and_secrets,
    validate_case_sensitive_links,
)
from post_deploy_hook import (
    test_url_endpoint
)

load_dotenv()

class CloudflareConfig:
    def __init__(self, args: argparse.Namespace):
        self.account_id = default_str(args.account_id)
        self.api_token = default_str(args.api_token)
        self.project_name = default_str(args.project_name)
        self.project_home = default_str(args.project_home)
        self.url = default_str(args.url)
        self.deployed_url = default_str(args.deployed_url)

def check_credentials(config: CloudflareConfig) -> Cloudflare:
    if not config.api_token:
        raise CloudflareAPIKeyException("CLOUDFLARE_API_TOKEN environment variable not set")
    if not config.url:
        raise CloudflareVerificationURLException("CLOUDFLARE_DEFAULT_URL environment variable not set")
    client = Cloudflare(api_token=config.api_token)
    res = client.accounts.tokens.verify(account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"])
    if res is None:
        raise CloudflareVerificationException("The provided API token/key does not give the user access to the verification API")
    res_str = json.loads(res.to_json())
    print(f"Cloudflare is: " + exp.green_str(res_str['status']))
    return client

def parse_args():
    parser = argparse.ArgumentParser(
        prog='clouddeploy',
        description='Deploys artifacts to Cloudflare')
    parser.add_argument('-a', '--account-id', default=default_str(os.environ.get("CLOUDFLARE_ACCOUNT_ID")), help='Cloudflare account ID')
    parser.add_argument('-k', '--api-token',default=read_api_token(), help='Cloudflare API key')
    parser.add_argument('-p', '--project-name', default=os.environ.get("CLOUDFLARE_PROJECT_NAME"), help='Name of project to deploy to workers')
    parser.add_argument("-ph", "--project-home", default=os.environ.get("CLOUDFLARE_PROJECT_LOCATION"), help='Cloudflare project home on the users local machine')
    parser.add_argument("-u", "--url", default=os.environ.get("CLOUDFLARE_DEFAULT_URL"), help='Cloudflare url to send requests too')
    parser.add_argument("-du", "--deployed-url", default=os.environ.get("CLOUDFLARE_DEPLOYED_URL"),
                        help='url you expect cloudflare to deploy too')
    return parser.parse_args()

def get_worker_script(cloud_client: Cloudflare, config: CloudflareConfig) -> str:
    for script in cloud_client.workers.scripts.list(account_id=config.account_id):
        if script.id == config.project_name:
            return script.to_json()
    return ""

def git_changes_present(config: CloudflareConfig):
    git = gm.GitManager.get_instance(config.project_home)
    if not git.cwd_has_changes() and not git.cwd_has_unstaged_changes():
        return False
    return True

def stage_local_changes():
    git = gm.GitManager.get_instance()
    git.git_add()
    git.git_commit()

def get_latest_deployment(client: Cloudflare, config: CloudflareConfig) -> dict | None:
    deploy = client.workers.scripts.deployments.list(
        account_id=config.account_id,
        script_name=config.project_name,
    )
    deployments = deploy.result.deployments
    if not deployments:
        return None
    # Cloudflare returns deployments newest-first, or max() by 'created_on'
    return max(deployments, key=lambda d: d["created_on"])

def publish_local_changes():
    git = gm.GitManager.get_instance()
    git.git_push(branch="master")
    res = git.git_rev_parse()
    if res[0] != 0:
        raise GitUnableToDetermineRevision("Git is unable to determine revision. Got following issue: {result}".format(result=res))
    return res[1]

def wait_for_deployment(client, config, previous_deploy_id, timeout_seconds, poll_interval):
    print(exp.cyan_str(f"Waiting for Cloudflare to build and deploy '{config.project_name}'..."))
    start_time = time.time()
    vals = []
    while time.time() - start_time < timeout_seconds:
        latest = get_latest_deployment(client, config)
        if latest and latest["id"] != previous_deploy_id:
            # Verify the new deployment has 100% traffic allocated
            versions = latest.get("versions", [])
            is_live = any(v.get("percentage") == 100 for v in versions)
            if is_live:
                elapsed = int(time.time() - start_time)
                print(exp.green_str(f"Deployment complete in {elapsed}s! (ID: {latest['id']})"))
                return latest
        elapsed = int(time.time() - start_time)
        print(f"  Still deploying... ({elapsed}s elapsed)")
        time.sleep(poll_interval)

    raise CloudflareDeploymentTimeoutException(f"Waiting for deployment to complete has timed out. waited {timeout_seconds} seconds.")


def run_pre_deploy_hooks(config: CloudflareConfig) -> None:
    project_home = config.project_home
    pdh_str = exp.underline_str("Pre-deploy hook:")

    # Hook 3: Prune unused fonts & unminified vendor duplicates
    pruned_count, pruned_bytes = prune_unused_assets(project_home)
    if pruned_count > 0:
        pruned_mb = pruned_bytes / (1024 * 1024)
        print(pdh_str + exp.green_str(f" Pruned {pruned_count} unused assets (saved {pruned_mb:.2f} MB)"))

    # Image optimization hook
    imgs_dir = os.path.join(project_home, "imgs")
    img_count, img_saved = optimize_images(imgs_dir)
    if img_count > 0:
        saved_mb = img_saved / (1024 * 1024)
        print(pdh_str + exp.green_str(f" Optimized {img_count} images (saved {saved_mb:.2f} MB)"))

    # Hook 2: Inject loading="lazy" and decoding="async" onto <img> tags
    html_files_mod, img_tags_mod = inject_lazy_loading_in_html(project_home)
    if img_tags_mod > 0:
        print(pdh_str + exp.green_str(f" Added lazy-loading to {img_tags_mod} <img> tags across {html_files_mod} HTML files"))

    # Hook 6: Auto-generate sitemap.xml and link in robots.txt
    site_url = os.environ.get("CLOUDFLARE_SITE_URL", "https://natebrowndev.com")
    if generate_sitemap(project_home, base_url=site_url):
        print(pdh_str + exp.green_str(" Updated sitemap.xml and robots.txt"))

    # Hook 5: Validate Cloudflare 25 MiB / 20k asset limits and block secret files
    valid_files = validate_asset_limits_and_secrets(project_home)
    print(pdh_str+ exp.cyan_str(f" Verified {valid_files} files against Cloudflare limits & secret rules"))

    # Hook 1: Validate all local HTML/CSS asset links (with exact case-sensitivity)
    checked_refs = validate_case_sensitive_links(project_home)
    print(pdh_str + exp.cyan_str(f" Validated {checked_refs} local HTML/CSS links (exact case match)"))

def run_post_deploy_hooks(config: CloudflareConfig):
    test_url_endpoint(config)

def cloud_deploy():
    try:
        args = parse_args()
        config = CloudflareConfig(args)
        client = check_credentials(config)
        validate_proj_name_is_cloudflare_registered(args, client, config)
        run_pre_deploy_hooks(config)
        changes_present = git_changes_present(config=config)
        if not changes_present:
            exp.print_no_changes_message()

        # 1. Snapshot the current deployment ID before pushing
        previous_deploy = get_latest_deployment(client, config)
        previous_deploy_id = previous_deploy["id"] if previous_deploy else None

        # 2. Stage, commit, and push local changes to trigger Cloudflare's Git build
        stage_local_changes()
        revision = publish_local_changes()
        print(f"Pushed Git revision: {exp.cyan_str(default_str(revision))}")

        # 3. Poll until Cloudflare creates and activates the new deployment
        finished = wait_for_deployment(
            client=client,
            config=config,
            previous_deploy_id=previous_deploy_id,
            timeout_seconds=360,
            poll_interval=20,
        )
        print("Deployed version:", finished["versions"][0]["version_id"])
    except Exception as e:
        exp.print_exception(e)


def validate_proj_name_is_cloudflare_registered(args: Namespace, client: Cloudflare, config: CloudflareConfig):
    proj = get_deployable_projects(client, config)
    if config.project_name not in proj:
        raise CloudflareProjectException(f"Cloudflare project not available: {args.project_name}")


def deployment_metadata():
    dt = datetime.datetime.now()
    dt_str = dt.strftime("%Y%m%d%H%M%S")
    return version_create_params.Metadata(compatibility_date=dt_str, main_module="clouddeploy")

def get_deployable_projects(client: Cloudflare, config: CloudflareConfig):
    project = []
    try:
        for x in client.workers.scripts.list(account_id=config.account_id):
            project.append(json.loads(x.to_json())['id'])
    except Exception as e:
        exp.print_exception(e)

    return project

class CloudflareAPIKeyException(Exception):
    """When the CLOUDFLARE_API_TOKEN is not available as part of the Environment Variables"""
    pass
class CloudflareVerificationURLException(Exception):
    """When the CLOUDFLARE_DEFAULT_URL is not available as part of the Environment Variables"""
    pass
class CloudflareVerificationException(Exception):
    """When the supplied API key/token does not give access to the required Cloudflare endpoints"""
    pass
class CloudflareProjectException(Exception):
    """When the project supplied does not exist in Cloudflare"""
    pass
class GitUnableToDetermineRevision(Exception):
    """When git is unable to determine the revision tag"""
    pass
class CloudflareDeploymentTimeoutException(Exception):
    """When a new Cloudflare deployment is not created or completed within the timeout window"""
    pass

def default_str(a_str : str | None) -> str:
    if a_str is None:
        return ""
    else:
        return a_str

def read_api_token() -> str:
        key = os.environ["CLOUDFLARE_API_TOKEN"]
        if key is None:
            return ""
        return key[key.find(":")+1:]


if __name__ == '__main__':
    cloud_deploy()


