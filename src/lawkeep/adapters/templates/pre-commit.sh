# lawkeep:begin (added by `lawkeep init`; delete this block to remove it)
# Shows law changes that did not go through lawkeep. It does not block the commit unless you
# ask it to:  git config lawkeep.precommit block
if command -v lawkeep >/dev/null 2>&1; then
  lawkeep hook pre-commit
  lawkeep_status=$?
elif [ -x "$(git rev-parse --show-toplevel)/.venv/bin/lawkeep" ]; then
  "$(git rev-parse --show-toplevel)/.venv/bin/lawkeep" hook pre-commit
  lawkeep_status=$?
else
  lawkeep_status=0
fi
if [ "$lawkeep_status" = 1 ]; then
  exit 1
fi
# lawkeep:end
