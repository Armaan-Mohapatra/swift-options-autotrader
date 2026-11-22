[CmdletBinding()]
param(
  [ValidateSet('Inventory','Plan')][string]$Mode = 'Inventory',
  [ValidateSet('Core','Expanded','All')][string]$Profile = 'Core'
)
# Read-only. No installs, no network calls, no settings changes, no services started or stopped.
$tools = @(
  @{ Name='Git'; Cmd='git'; Tier='Core'; Url='https://github.com/git/git' },
  @{ Name='GitHub CLI'; Cmd='gh'; Tier='Core'; Url='https://github.com/cli/cli' },
  @{ Name='RTK'; Cmd='rtk'; Tier='Core'; Url='https://github.com/rtk-ai/rtk' },
  @{ Name='Node'; Cmd='node'; Tier='Core'; Url='https://github.com/nodejs/node' },
  @{ Name='Codex'; Cmd='codex'; Tier='Core'; Url='https://github.com/openai/codex' },
  @{ Name='Claude Code'; Cmd='claude'; Tier='Core'; Url='https://code.claude.com/docs' },
  @{ Name='Gitleaks'; Cmd='gitleaks'; Tier='Core'; Url='https://github.com/gitleaks/gitleaks' },
  @{ Name='just'; Cmd='just'; Tier='Core'; Url='https://github.com/casey/just' },
  @{ Name='ast-grep'; Cmd='sg'; Tier='Expanded'; Url='https://github.com/ast-grep/ast-grep' },
  @{ Name='Beads'; Cmd='bd'; Tier='Expanded'; Url='https://github.com/gastownhall/beads' },
  @{ Name='OpenSpec'; Cmd='openspec'; Tier='Expanded'; Url='https://github.com/Fission-AI/OpenSpec' },
  @{ Name='Repomix'; Cmd='repomix'; Tier='Expanded'; Url='https://github.com/yamadashy/repomix' },
  @{ Name='Lazygit'; Cmd='lazygit'; Tier='Expanded'; Url='https://github.com/jesseduffield/lazygit' },
  @{ Name='mise'; Cmd='mise'; Tier='Expanded'; Url='https://github.com/jdx/mise' },
  @{ Name='cargo-nextest'; Cmd='cargo-nextest'; Tier='All'; Url='https://github.com/nextest-rs/nextest' },
  @{ Name='cargo-deny'; Cmd='cargo-deny'; Tier='All'; Url='https://github.com/EmbarkStudios/cargo-deny' },
  @{ Name='cargo-llvm-cov'; Cmd='cargo-llvm-cov'; Tier='All'; Url='https://github.com/taiki-e/cargo-llvm-cov' },
  @{ Name='uv'; Cmd='uv'; Tier='All'; Url='https://github.com/astral-sh/uv' },
  @{ Name='ruff'; Cmd='ruff'; Tier='All'; Url='https://github.com/astral-sh/ruff' }
)
$level = @{ Core=1; Expanded=2; All=3 }
$visible = $tools | Where-Object { $level[$_.Tier] -le $level[$Profile] }
$rows = foreach ($t in $visible) {
  $found = Get-Command $t.Cmd -ErrorAction SilentlyContinue | Select-Object -First 1
  [pscustomobject]@{
    Tool = $t.Name
    Present = [bool]$found
    Command = $t.Cmd
    Scope = $t.Tier
    Reference = $t.Url
  }
}
if ($Mode -eq 'Inventory') {
  $rows | Format-Table Tool,Present,Command,Scope -AutoSize
  Write-Host "This only checks PATH. Plugins, IDE extensions, hooks, and software outside PATH need separate checks."
} else {
  $rows | Format-Table Tool,Present,Reference -Wrap
  Write-Host "PLAN ONLY: inspect upstream releases, licenses, installed hooks and data-access before any install."
  Write-Host "No software or services were changed."
}
