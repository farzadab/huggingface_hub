# Copyright 2023 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Contains an helper to get the token from machine (env variable, secret or config file)."""

import configparser
import logging
import os
import warnings
from pathlib import Path
from threading import Lock
from typing import Dict, Optional

from .. import constants
from ._runtime import is_colab_enterprise, is_google_colab


_IS_GOOGLE_COLAB_CHECKED = False
_GOOGLE_COLAB_SECRET_LOCK = Lock()
_GOOGLE_COLAB_SECRET: Optional[str] = None

logger = logging.getLogger(__name__)


def get_token() -> Optional[str]:
    """
    Get token if user is logged in.

    Note: in most cases, you should use [`huggingface_hub.utils.build_hf_headers`] instead. This method is only useful
          if you want to retrieve the token for other purposes than sending an HTTP request.

    Token is retrieved in priority from the `HF_TOKEN` environment variable. Otherwise, we read the token file located
    in the Hugging Face home folder. Returns None if user is not logged in. To log in, use [`login`] or
    `huggingface-cli login`.

    Returns:
        `str` or `None`: The token, `None` if it doesn't exist.
    """
    return _get_token_from_google_colab() or _get_token_from_environment() or _get_token_from_file()


def _get_token_from_google_colab() -> Optional[str]:
    """Get token from Google Colab secrets vault using `google.colab.userdata.get(...)`.

    Token is read from the vault only once per session and then stored in a global variable to avoid re-requesting
    access to the vault.
    """
    # If it's not a Google Colab or it's Colab Enterprise, fallback to environment variable or token file authentication
    if not is_google_colab() or is_colab_enterprise():
        return None

    # `google.colab.userdata` is not thread-safe
    # This can lead to a deadlock if multiple threads try to access it at the same time
    # (typically when using `snapshot_download`)
    # => use a lock
    # See https://github.com/huggingface/huggingface_hub/issues/1952 for more details.
    with _GOOGLE_COLAB_SECRET_LOCK:
        global _GOOGLE_COLAB_SECRET
        global _IS_GOOGLE_COLAB_CHECKED

        if _IS_GOOGLE_COLAB_CHECKED:  # request access only once
            return _GOOGLE_COLAB_SECRET

        try:
            from google.colab import userdata  # type: ignore
            from google.colab.errors import Error as ColabError  # type: ignore
        except ImportError:
            return None

        try:
            token = userdata.get("HF_TOKEN")
            _GOOGLE_COLAB_SECRET = _clean_token(token)
        except userdata.NotebookAccessError:
            # Means the user has a secret call `HF_TOKEN` and got a popup "please grand access to HF_TOKEN" and refused it
            # => warn user but ignore error => do not re-request access to user
            warnings.warn(
                "\nAccess to the secret `HF_TOKEN` has not been granted on this notebook."
                "\nYou will not be requested again."
                "\nPlease restart the session if you want to be prompted again."
            )
            _GOOGLE_COLAB_SECRET = None
        except userdata.SecretNotFoundError:
            # Means the user did not define a `HF_TOKEN` secret => warn
            warnings.warn(
                "\nThe secret `HF_TOKEN` does not exist in your Colab secrets."
                "\nTo authenticate with the Hugging Face Hub, create a token in your settings tab "
                "(https://huggingface.co/settings/tokens), set it as secret in your Google Colab and restart your session."
                "\nYou will be able to reuse this secret in all of your notebooks."
                "\nPlease note that authentication is recommended but still optional to access public models or datasets."
            )
            _GOOGLE_COLAB_SECRET = None
        except ColabError as e:
            # Something happen but we don't know what => recommend to open a GitHub issue
            warnings.warn(
                f"\nError while fetching `HF_TOKEN` secret value from your vault: '{str(e)}'."
                "\nYou are not authenticated with the Hugging Face Hub in this notebook."
                "\nIf the error persists, please let us know by opening an issue on GitHub "
                "(https://github.com/huggingface/huggingface_hub/issues/new)."
            )
            _GOOGLE_COLAB_SECRET = None

        _IS_GOOGLE_COLAB_CHECKED = True
        return _GOOGLE_COLAB_SECRET


def _get_token_from_environment() -> Optional[str]:
    # `HF_TOKEN` has priority (keep `HUGGING_FACE_HUB_TOKEN` for backward compatibility)
    return _clean_token(os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"))


def _get_token_from_file() -> Optional[str]:
    try:
        return _clean_token(Path(constants.HF_TOKEN_PATH).read_text())
    except FileNotFoundError:
        return None


def _get_profiles() -> Dict[str, str]:
    """
    Returns the parsed INI file containing the auth profiles.
    The file is located at `HF_PROFILES_PATH`, defaulting to `~/.cache/huggingface/profiles`.
    If the file does not exist,

    Returns: `Dict[str, str]`
        Key is the profile name and value is the token.

    Raises:
        FileNotFoundError: If the profiles file does not exist.
    """
    profiles_path = Path(constants.HF_PROFILES_PATH)
    if not profiles_path.exists():
        profiles = {}
    config = configparser.ConfigParser()
    try:
        config.read(profiles_path)
        profiles = {profile: config.get(profile, "hf_token") for profile in config.sections()}
    except configparser.Error as e:
        logger.error(f"Error parsing profiles file: {e}")
        profiles = {}
    return profiles


def _save_profiles(profiles: Dict[str, str]) -> None:
    """
    Saves the given configuration to the profiles file.

    Args:
        profiles (`Dict[str, str]`):
            The profiles to save. Key is the profile name and value is the token.
    """
    profiles_path = Path(constants.HF_PROFILES_PATH)

    # Write the profiles into an INI file
    config = configparser.ConfigParser()
    for profile_name, token in profiles.items():
        config.add_section(profile_name)
        config.set(profile_name, "hf_token", token)

    profiles_path.parent.mkdir(parents=True, exist_ok=True)
    with profiles_path.open("w+") as config_file:
        config.write(config_file)


def _get_token_from_profile(profile_name: str = "default") -> Optional[str]:
    """
    Get the token from the given profile.

    Args:
        profile_name (`str`, *optional*, defaults to `"default"`):
            The name of the profile to get the token from.

    Returns:
        `str` or `None`: The token, `None` if it doesn't exist.

    """
    profiles = _get_profiles()
    if profile_name not in profiles:
        return None
    return _clean_token(profiles[profile_name])


def _save_token_to_profile(token: str, profile_name: str = "default") -> None:
    """
    Save the given token to the given profile.

    If the profiles file does not exist, it will be created.
    Args:
        token (`str`):
            The token to save.
        profile_name (`str`, *optional*, defaults to `"default"`):
            The name of the profile to save the token to.
    """
    profiles_path = Path(constants.HF_PROFILES_PATH)
    profiles = _get_profiles()
    profiles[profile_name] = token
    _save_profiles(profiles)
    print(f"Your profile `{profile_name}` has been saved to {profiles_path}")


def _clean_token(token: Optional[str]) -> Optional[str]:
    """Clean token by removing trailing and leading spaces and newlines.

    If token is an empty string, return None.
    """
    if token is None:
        return None
    return token.replace("\r", "").replace("\n", "").strip() or None
