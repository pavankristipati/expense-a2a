# Loads the lab's secrets from macOS Keychain into this Terminal window only.
# Run it with:  source load_secrets.sh
# This file holds no secrets itself, so it is safe to keep in the repo.
export ANTHROPIC_API_KEY=$(security find-generic-password -a "$USER" -s anthropic-api-key -w)
export MANAGER_TOKEN=$(security find-generic-password -a "$USER" -s policy-agent-manager-token -w)
export EVAL_TOKEN=$(security find-generic-password -a "$USER" -s policy-agent-eval-token -w)
echo "Loaded: Anthropic key, manager token, eval token"
