#Requires -Version 5.1
<#
.SYNOPSIS
    ClawFactory Installer for Windows

.DESCRIPTION
    Sets up ClawFactory - a secure runtime for AI agents.
    Configures Docker containers, secrets, and bot repositories.

.EXAMPLE
    .\install.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

# ---------------------------
# ClawFactory Install Script (Windows)
# ---------------------------

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SecretsDir = Join-Path $ScriptDir "secrets"
$BotReposDir = Join-Path $ScriptDir "bot_repos"
$ConfigFile = Join-Path $ScriptDir ".clawfactory.conf"

# Global variables
$script:INSTANCE_NAME = ""
$script:GITHUB_USERNAME = ""
$script:GITHUB_ORG = ""
$script:SANDBOX_ENABLED = "false"
$script:MODE = "offline"
$script:USE_SAVED_SECRETS = $false

# Channel tokens
$script:DISCORD_BOT_TOKEN = ""
$script:TELEGRAM_BOT_TOKEN = ""
$script:SLACK_BOT_TOKEN = ""
$script:SLACK_APP_TOKEN = ""

# AI Provider keys
$script:ANTHROPIC_API_KEY = ""
$script:MOONSHOT_API_KEY = ""
$script:OPENAI_API_KEY = ""
$script:GEMINI_API_KEY = ""
$script:OLLAMA_ENABLED = ""
$script:OPENROUTER_API_KEY = ""
$script:BRAVE_API_KEY = ""
$script:ELEVENLABS_API_KEY = ""
$script:PRIMARY_MODEL = ""

# Memory settings
$script:MEMORY_SEARCH_ENABLED = "false"
$script:MEMORY_SEARCH_PROVIDER = ""
$script:MEMORY_SEARCH_MODEL = ""
$script:MEMORY_SEARCH_BASE_URL = ""
$script:MEMORY_SEARCH_API_KEY = ""

# GitHub settings
$script:GITHUB_WEBHOOK_SECRET = ""
$script:GITHUB_ALLOWED_ACTORS = ""
$script:GITHUB_BOT_REPO = ""
$script:GITHUB_TOKEN = ""
$script:GITHUB_REPO_OWNER = ""

# ============================================================
# Helper Functions
# ============================================================

function Write-Info { param($Message) Write-Host "-> " -ForegroundColor Green -NoNewline; Write-Host $Message }
function Write-Warn { param($Message) Write-Host "!! " -ForegroundColor Yellow -NoNewline; Write-Host $Message }
function Write-Err { param($Message) Write-Host "X " -ForegroundColor Red -NoNewline; Write-Host $Message }
function Write-Success { param($Message) Write-Host "[OK] " -ForegroundColor Green -NoNewline; Write-Host $Message }

function Exit-WithError {
    param($Message)
    Write-Err $Message
    exit 1
}

function Test-Command {
    param($Command)
    $null = Get-Command $Command -ErrorAction SilentlyContinue
    return $?
}

function Get-RandomHex {
    param([int]$Bytes = 32)
    $bytes = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    return [BitConverter]::ToString($bytes).Replace("-", "").ToLower()
}

function Validate-InstanceName {
    param($Name)

    if ([string]::IsNullOrEmpty($Name)) {
        return "Instance name cannot be empty"
    }
    if ($Name.Length -gt 32) {
        return "Instance name must be 32 characters or less"
    }
    if ($Name -match "^-" -or $Name -match "-$") {
        return "Instance name cannot start or end with a hyphen"
    }
    if ($Name -cmatch "[A-Z]") {
        return "Instance name must be lowercase"
    }
    if ($Name -notmatch "^[a-z0-9][a-z0-9-]*[a-z0-9]$" -and $Name -notmatch "^[a-z0-9]$") {
        return "Instance name can only contain lowercase letters, numbers, and hyphens"
    }
    return $null
}

function Read-SecurePrompt {
    param(
        [string]$Prompt,
        [string]$Default = ""
    )

    if ($Default) {
        $Prompt = "$Prompt [****saved****]"
    }

    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $value = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

    if ([string]::IsNullOrEmpty($value) -and $Default) {
        return $Default
    }
    return $value
}

function Read-Prompt {
    param(
        [string]$Prompt,
        [string]$Default = ""
    )

    if ($Default) {
        $Prompt = "$Prompt [$Default]"
    }

    $value = Read-Host -Prompt $Prompt
    if ([string]::IsNullOrEmpty($value) -and $Default) {
        return $Default
    }
    return $value
}

function Load-EnvValue {
    param(
        [string]$Key,
        [string]$Instance = $script:INSTANCE_NAME
    )

    $gatewayKeys = @("DISCORD_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
                     "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                     "OLLAMA_API_KEY", "OPENROUTER_API_KEY", "BRAVE_API_KEY", "ELEVENLABS_API_KEY",
                     "OPENCLAW_GATEWAY_TOKEN")

    $controllerKeys = @("GITHUB_WEBHOOK_SECRET", "ALLOWED_MERGE_ACTORS", "CONTROLLER_API_TOKEN", "GITHUB_TOKEN")

    if ($gatewayKeys -contains $Key) {
        $file = Join-Path $SecretsDir "$Instance\gateway.env"
    } elseif ($controllerKeys -contains $Key) {
        $file = Join-Path $SecretsDir "$Instance\controller.env"
    } else {
        return $null
    }

    if (-not (Test-Path $file)) {
        return $null
    }

    $content = Get-Content $file -ErrorAction SilentlyContinue
    foreach ($line in $content) {
        if ($line -match "^$Key=(.*)$") {
            return $Matches[1]
        }
    }
    return $null
}

function Load-Config {
    if (Test-Path $ConfigFile) {
        $content = Get-Content $ConfigFile
        foreach ($line in $content) {
            if ($line -match '^(\w+)="?([^"]*)"?$') {
                $name = $Matches[1]
                $value = $Matches[2]
                Set-Variable -Name $name -Value $value -Scope Script
            }
        }
    }
}

function Save-Config {
    $content = @"
# ClawFactory Instance Configuration
INSTANCE_NAME="$script:INSTANCE_NAME"
GITHUB_USERNAME="$script:GITHUB_USERNAME"
GITHUB_ORG="$script:GITHUB_ORG"
SANDBOX_ENABLED="$script:SANDBOX_ENABLED"
"@
    Set-Content -Path $ConfigFile -Value $content

    # Also save .env for docker-compose
    $envContent = @"
# Docker Compose environment (auto-generated)
INSTANCE_NAME=$script:INSTANCE_NAME
COMPOSE_PROJECT_NAME=clawfactory-$script:INSTANCE_NAME
GITHUB_USERNAME=$script:GITHUB_USERNAME
GITHUB_ORG=$script:GITHUB_ORG
SANDBOX_ENABLED=$script:SANDBOX_ENABLED
"@
    Set-Content -Path (Join-Path $ScriptDir ".env") -Value $envContent
}

# ============================================================
# Pre-flight Checks
# ============================================================
function Test-Preflight {
    Write-Info "Checking dependencies..."

    if (-not (Test-Command "docker")) {
        Exit-WithError "Docker is not installed. Please install Docker Desktop for Windows."
    }

    if (-not (Test-Command "git")) {
        Exit-WithError "Git is not installed. Please install Git for Windows."
    }

    # Check Docker is running
    try {
        $null = docker info 2>$null
        if ($LASTEXITCODE -ne 0) {
            Exit-WithError "Docker is not running. Please start Docker Desktop and try again."
        }
    } catch {
        Exit-WithError "Docker is not running. Please start Docker Desktop and try again."
    }

    # GitHub CLI is optional - only needed for online mode
    $script:GH_AVAILABLE = $false
    if (Test-Command "gh") {
        $null = gh auth status 2>$null
        if ($LASTEXITCODE -eq 0) {
            $script:GH_AVAILABLE = $true
            Write-Success "GitHub CLI authenticated"
        } else {
            Write-Info "GitHub CLI installed but not authenticated"
            Write-Info "  Run 'gh auth login' to enable GitHub integration"
        }
    } else {
        Write-Info "GitHub CLI not installed (GitHub integration unavailable)"
        Write-Info "  Install from: https://cli.github.com/"
    }

    # Sandbox not available on Windows (Sysbox is Linux-only)
    $script:SYSBOX_AVAILABLE = $false
    Write-Info "Sandbox mode is not available on Windows (Sysbox requires Linux)"

    Write-Success "Core dependencies satisfied"
}

# ============================================================
# Configure Sandbox Mode
# ============================================================
function Configure-Sandbox {
    Write-Host ""
    Write-Host "=== Sandbox Mode ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Sandbox mode is not available on Windows."
    Write-Host "Sysbox (required for sandboxing) only supports Linux."
    Write-Host ""
    Write-Host "Tools will run directly on the gateway container."
    Write-Host "For sandboxed execution, deploy on a Linux host."
    Write-Host ""
    $script:SANDBOX_ENABLED = "false"
}

# ============================================================
# Configure Secrets
# ============================================================
function Configure-Secrets {
    # Create secrets directory
    if (-not (Test-Path $SecretsDir)) {
        New-Item -ItemType Directory -Path $SecretsDir -Force | Out-Null
    }

    # Load existing config
    Load-Config

    # Instance name configuration
    Write-Host ""
    Write-Host "=== Instance Name ===" -ForegroundColor Cyan
    Write-Host "This identifies your ClawFactory instance (e.g., 'bot1', 'bot2', 'prod-agent')"
    Write-Host "Used for container names and token storage."
    Write-Host ""

    $defaultInstance = if ($script:INSTANCE_NAME) { $script:INSTANCE_NAME } else { "bot1" }

    while ($true) {
        $script:INSTANCE_NAME = Read-Prompt "Instance name" $defaultInstance
        $validationError = Validate-InstanceName $script:INSTANCE_NAME
        if (-not $validationError) {
            break
        }
        Write-Err $validationError
        Write-Host "Please try again."
    }
    Save-Config
    Write-Success "Instance name: $script:INSTANCE_NAME"

    # Check for existing secrets
    $instanceSecretsDir = Join-Path $SecretsDir $script:INSTANCE_NAME

    if (Test-Path (Join-Path $instanceSecretsDir "gateway.env")) {
        Write-Host ""
        Write-Host "=== Existing Configuration Found ===" -ForegroundColor Cyan
        Write-Host "Found saved secrets for instance '$script:INSTANCE_NAME'"
        Write-Host ""

        # Show what's configured
        $gatewayEnv = Join-Path $instanceSecretsDir "gateway.env"
        $hasDiscord = (Select-String -Path $gatewayEnv -Pattern "DISCORD_BOT_TOKEN=" -Quiet) -eq $true
        $hasAnthropic = (Select-String -Path $gatewayEnv -Pattern "ANTHROPIC_API_KEY=" -Quiet) -eq $true
        $hasMoonshot = (Select-String -Path $gatewayEnv -Pattern "MOONSHOT_API_KEY=" -Quiet) -eq $true
        $hasOpenai = (Select-String -Path $gatewayEnv -Pattern "OPENAI_API_KEY=" -Quiet) -eq $true

        Write-Host "Saved configuration:"
        if ($hasDiscord) { Write-Host "  - Discord bot token" }
        if ($hasAnthropic) { Write-Host "  - Anthropic API key" }
        if ($hasMoonshot) { Write-Host "  - Moonshot/Kimi API key" }
        if ($hasOpenai) { Write-Host "  - OpenAI API key" }
        Write-Host ""

        $useSaved = Read-Prompt "Use saved configuration? [Y/n]" "Y"
        if ($useSaved -notmatch "^[Nn]") {
            $script:USE_SAVED_SECRETS = $true
            Write-Success "Using saved configuration"

            # Load all saved values
            $script:DISCORD_BOT_TOKEN = Load-EnvValue "DISCORD_BOT_TOKEN"
            $script:TELEGRAM_BOT_TOKEN = Load-EnvValue "TELEGRAM_BOT_TOKEN"
            $script:SLACK_BOT_TOKEN = Load-EnvValue "SLACK_BOT_TOKEN"
            $script:SLACK_APP_TOKEN = Load-EnvValue "SLACK_APP_TOKEN"
            $script:ANTHROPIC_API_KEY = Load-EnvValue "ANTHROPIC_API_KEY"
            $script:MOONSHOT_API_KEY = Load-EnvValue "MOONSHOT_API_KEY"
            $script:OPENAI_API_KEY = Load-EnvValue "OPENAI_API_KEY"
            $script:GEMINI_API_KEY = Load-EnvValue "GEMINI_API_KEY"
            $script:OLLAMA_ENABLED = Load-EnvValue "OLLAMA_API_KEY"
            if ($script:OLLAMA_ENABLED) { $script:OLLAMA_ENABLED = "true" }
            $script:OPENROUTER_API_KEY = Load-EnvValue "OPENROUTER_API_KEY"
            $script:BRAVE_API_KEY = Load-EnvValue "BRAVE_API_KEY"
            $script:ELEVENLABS_API_KEY = Load-EnvValue "ELEVENLABS_API_KEY"

            # Set primary model
            if ($script:ANTHROPIC_API_KEY) {
                $script:PRIMARY_MODEL = "anthropic/claude-sonnet-4-20250514"
            } elseif ($script:MOONSHOT_API_KEY) {
                $script:PRIMARY_MODEL = "moonshot/kimi-k2.5"
            } elseif ($script:OPENAI_API_KEY) {
                $script:PRIMARY_MODEL = "openai/gpt-4o"
            } elseif ($script:OLLAMA_ENABLED) {
                $script:PRIMARY_MODEL = "ollama/llama3.2"
            }

            # Default settings
            $script:MODE = "offline"
            $script:MEMORY_SEARCH_ENABLED = "true"
            $script:MEMORY_SEARCH_PROVIDER = "openclaw"
            $script:MEMORY_SEARCH_MODEL = "local"
            $script:GITHUB_USERNAME = ""
            $script:GITHUB_WEBHOOK_SECRET = ""
            $script:GITHUB_ALLOWED_ACTORS = ""
            $script:GITHUB_BOT_REPO = ""

            return
        } else {
            Write-Info "Starting fresh configuration"
        }
    }

    # GitHub Integration - only offer if GitHub CLI is available and authenticated
    if ($script:GH_AVAILABLE) {
        Write-Host ""
        Write-Host "=== GitHub Integration ===" -ForegroundColor Cyan
        Write-Host "Configure GitHub for PR-based promotion workflow?"
        Write-Host ""
        Write-Host "  no  - Use Controller UI only (recommended)"
        Write-Host "  yes - Configure GitHub webhooks and PR workflow"
        Write-Host ""
        Write-Warn "Note: GitHub integration is not fully implemented yet."
        Write-Host ""

        $configureGithub = Read-Prompt "Configure GitHub? [y/N]" "N"
        if ($configureGithub -match "^[Yy]") {
            $script:MODE = "online"
        } else {
            $script:MODE = "offline"
        }
    } else {
        Write-Host ""
        Write-Info "Running in local mode (GitHub CLI not available)"
        $script:MODE = "offline"
    }

    # Channel Configuration
    Write-Host ""
    Write-Host "=== Channel Configuration ===" -ForegroundColor Cyan
    Write-Host "Select which chat channels to configure:"
    Write-Host ""
    Write-Host "  1) Discord    - Discord bot"
    Write-Host "  2) Telegram   - Telegram bot"
    Write-Host "  3) Slack      - Slack app"
    Write-Host ""
    Write-Host "Enter numbers separated by spaces (e.g., '1 2' for Discord + Telegram)"
    Write-Host ""

    $channelSelection = Read-Prompt "Channels" "1"
    if (-not $channelSelection) { $channelSelection = "1" }

    $channels = $channelSelection -split '\s+'

    foreach ($channel in $channels) {
        switch ($channel) {
            "1" {
                Write-Host ""
                Write-Host "--- Discord ---" -ForegroundColor Yellow
                Write-Host "Create a bot at: https://discord.com/developers/applications"
                $saved = Load-EnvValue "DISCORD_BOT_TOKEN"
                if ($saved) {
                    $script:DISCORD_BOT_TOKEN = $saved
                    Write-Success "Discord bot token (saved)"
                } else {
                    $script:DISCORD_BOT_TOKEN = Read-SecurePrompt "Discord bot token"
                }
            }
            "2" {
                Write-Host ""
                Write-Host "--- Telegram ---" -ForegroundColor Yellow
                Write-Host "Create a bot via @BotFather: https://t.me/botfather"
                $saved = Load-EnvValue "TELEGRAM_BOT_TOKEN"
                if ($saved) {
                    $script:TELEGRAM_BOT_TOKEN = $saved
                    Write-Success "Telegram bot token (saved)"
                } else {
                    $script:TELEGRAM_BOT_TOKEN = Read-SecurePrompt "Telegram bot token"
                }
            }
            "3" {
                Write-Host ""
                Write-Host "--- Slack ---" -ForegroundColor Yellow
                Write-Host "Create an app at: https://api.slack.com/apps"
                $saved = Load-EnvValue "SLACK_BOT_TOKEN"
                if ($saved) {
                    $script:SLACK_BOT_TOKEN = $saved
                    Write-Success "Slack bot token (saved)"
                } else {
                    $script:SLACK_BOT_TOKEN = Read-SecurePrompt "Slack bot token (xoxb-...)"
                    $script:SLACK_APP_TOKEN = Read-SecurePrompt "Slack app token (xapp-...)"
                }
            }
        }
    }

    # AI Provider Configuration
    Configure-AIProviders
}

# ============================================================
# Configure AI Providers
# ============================================================
function Configure-AIProviders {
    Write-Host ""
    Write-Host "=== AI Provider Selection ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Select which AI providers to configure:"
    Write-Host ""
    Write-Host "  1) Anthropic (Claude)      - Claude Sonnet 4 (recommended)"
    Write-Host "  2) Kimi K2.5 (Moonshot)    - 256k context, competitive pricing"
    Write-Host "  3) OpenAI (GPT-4)          - GPT models"
    Write-Host "  4) Google (Gemini)         - Gemini models"
    Write-Host "  5) Ollama (Local)          - Run models locally"
    Write-Host "  6) OpenRouter              - Multi-provider gateway"
    Write-Host "  7) Brave Search            - Web search API"
    Write-Host "  8) ElevenLabs              - Text-to-speech API"
    Write-Host ""
    Write-Host "Enter numbers separated by spaces (e.g., '1 5 7' for Anthropic + Ollama + Brave)"
    Write-Host "Press Enter for default: Anthropic only"
    Write-Host ""

    $providerSelection = Read-Prompt "Providers" "1"
    if (-not $providerSelection) { $providerSelection = "1" }

    $providers = $providerSelection -split '\s+'

    foreach ($provider in $providers) {
        switch ($provider) {
            "1" {
                Write-Host ""
                Write-Host "--- Anthropic (Claude) ---" -ForegroundColor Yellow
                Write-Host "Get your API key at: https://console.anthropic.com/settings/keys"
                $saved = Load-EnvValue "ANTHROPIC_API_KEY"
                if ($saved) {
                    $script:ANTHROPIC_API_KEY = $saved
                    Write-Success "Anthropic API key (saved)"
                } else {
                    $script:ANTHROPIC_API_KEY = Read-SecurePrompt "Anthropic API key"
                }
            }
            "2" {
                Write-Host ""
                Write-Host "--- Kimi K2.5 (Moonshot AI) ---" -ForegroundColor Yellow
                Write-Host "Get your API key at: https://platform.moonshot.ai/console/api-keys"
                Write-Host "Model: kimi-k2.5 (256k context)"
                $saved = Load-EnvValue "MOONSHOT_API_KEY"
                if ($saved) {
                    $script:MOONSHOT_API_KEY = $saved
                    Write-Success "Kimi API key (saved)"
                } else {
                    $script:MOONSHOT_API_KEY = Read-SecurePrompt "Kimi/Moonshot API key"
                }
            }
            "3" {
                Write-Host ""
                Write-Host "--- OpenAI (GPT-4) ---" -ForegroundColor Yellow
                Write-Host "Get your API key at: https://platform.openai.com/api-keys"
                $saved = Load-EnvValue "OPENAI_API_KEY"
                if ($saved) {
                    $script:OPENAI_API_KEY = $saved
                    Write-Success "OpenAI API key (saved)"
                } else {
                    $script:OPENAI_API_KEY = Read-SecurePrompt "OpenAI API key"
                }
            }
            "4" {
                Write-Host ""
                Write-Host "--- Google (Gemini) ---" -ForegroundColor Yellow
                Write-Host "Get your API key at: https://aistudio.google.com/app/apikey"
                $saved = Load-EnvValue "GEMINI_API_KEY"
                if ($saved) {
                    $script:GEMINI_API_KEY = $saved
                    Write-Success "Gemini API key (saved)"
                } else {
                    $script:GEMINI_API_KEY = Read-SecurePrompt "Gemini API key"
                }
            }
            "5" {
                Write-Host ""
                Write-Host "--- Ollama (Local Models) ---" -ForegroundColor Yellow
                Write-Host "Run models locally with Ollama: https://ollama.ai"
                Write-Host "Make sure Ollama is running on your machine."
                $script:OLLAMA_ENABLED = "true"
                Write-Success "Ollama will be configured"
            }
            "6" {
                Write-Host ""
                Write-Host "--- OpenRouter ---" -ForegroundColor Yellow
                Write-Host "Get your API key at: https://openrouter.ai/keys"
                $saved = Load-EnvValue "OPENROUTER_API_KEY"
                if ($saved) {
                    $script:OPENROUTER_API_KEY = $saved
                    Write-Success "OpenRouter API key (saved)"
                } else {
                    $script:OPENROUTER_API_KEY = Read-SecurePrompt "OpenRouter API key"
                }
            }
            "7" {
                Write-Host ""
                Write-Host "--- Brave Search ---" -ForegroundColor Yellow
                Write-Host "Get your API key at: https://brave.com/search/api/"
                $saved = Load-EnvValue "BRAVE_API_KEY"
                if ($saved) {
                    $script:BRAVE_API_KEY = $saved
                    Write-Success "Brave API key (saved)"
                } else {
                    $script:BRAVE_API_KEY = Read-SecurePrompt "Brave Search API key"
                }
            }
            "8" {
                Write-Host ""
                Write-Host "--- ElevenLabs ---" -ForegroundColor Yellow
                Write-Host "Get your API key at: https://elevenlabs.io/app/settings/api-keys"
                $saved = Load-EnvValue "ELEVENLABS_API_KEY"
                if ($saved) {
                    $script:ELEVENLABS_API_KEY = $saved
                    Write-Success "ElevenLabs API key (saved)"
                } else {
                    $script:ELEVENLABS_API_KEY = Read-SecurePrompt "ElevenLabs API key"
                }
            }
        }
    }

    # Determine primary model
    Write-Host ""
    if ($script:ANTHROPIC_API_KEY) {
        $script:PRIMARY_MODEL = "anthropic/claude-sonnet-4-20250514"
        Write-Info "Primary model: Claude Sonnet 4 (Anthropic)"
    } elseif ($script:MOONSHOT_API_KEY) {
        $script:PRIMARY_MODEL = "moonshot/kimi-k2.5"
        Write-Info "Primary model: Kimi K2.5 (Moonshot)"
    } elseif ($script:OPENAI_API_KEY) {
        $script:PRIMARY_MODEL = "openai/gpt-4o"
        Write-Info "Primary model: GPT-4o (OpenAI)"
    } elseif ($script:GEMINI_API_KEY) {
        $script:PRIMARY_MODEL = "google/gemini-2.0-flash"
        Write-Info "Primary model: Gemini 2.0 Flash (Google)"
    } elseif ($script:OPENROUTER_API_KEY) {
        $script:PRIMARY_MODEL = "openrouter/anthropic/claude-sonnet-4"
        Write-Info "Primary model: Claude Sonnet 4 via OpenRouter"
    } elseif ($script:OLLAMA_ENABLED) {
        $script:PRIMARY_MODEL = "ollama/llama3.2"
        Write-Info "Primary model: Llama 3.2 (Ollama local)"
    } else {
        $script:PRIMARY_MODEL = ""
        Write-Warn "No AI provider configured - bot will not be functional"
    }

    Write-Success "AI providers configured"

    # Configure vector memory
    Configure-VectorMemory
}

# ============================================================
# Configure Vector Memory
# ============================================================
function Configure-VectorMemory {
    Write-Host ""
    Write-Host "=== Vector Memory Search ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Vector memory enables semantic search over conversation history."
    Write-Host "This requires an embedding model to convert text to vectors."
    Write-Host ""

    $enableMemory = Read-Prompt "Enable vector memory search? [Y/n]" "Y"
    if ($enableMemory -match "^[Nn]") {
        $script:MEMORY_SEARCH_ENABLED = "false"
        Write-Warn "Vector memory disabled"
        return
    }

    Write-Host ""
    Write-Host "Select embedding provider:"
    Write-Host ""
    Write-Host "  1) OpenClaw (Local)    - Built-in embeddings, no external service needed"
    Write-Host "  2) Ollama (Local)      - nomic-embed-text, free, runs locally"
    Write-Host "  3) OpenAI              - text-embedding-3-small, high quality"
    Write-Host ""

    $embedSelection = Read-Prompt "Select provider" "1"
    if (-not $embedSelection) { $embedSelection = "1" }

    switch ($embedSelection) {
        "1" {
            $script:MEMORY_SEARCH_ENABLED = "true"
            $script:MEMORY_SEARCH_PROVIDER = "openclaw"
            $script:MEMORY_SEARCH_MODEL = "local"
            Write-Success "Vector memory: OpenClaw (local embeddings)"
        }
        "2" {
            $script:MEMORY_SEARCH_ENABLED = "true"
            $script:MEMORY_SEARCH_PROVIDER = "ollama"
            $script:MEMORY_SEARCH_MODEL = "nomic-embed-text"
            $script:MEMORY_SEARCH_BASE_URL = "http://host.docker.internal:11434/v1"
            $script:MEMORY_SEARCH_API_KEY = "ollama-local"
            Write-Success "Vector memory: Ollama (nomic-embed-text)"
            Write-Host ""
            Write-Info "Make sure to pull the embedding model:"
            Write-Host "  ollama pull nomic-embed-text"
        }
        "3" {
            if (-not $script:OPENAI_API_KEY) {
                Write-Host ""
                Write-Host "OpenAI API key required for embeddings."
                Write-Host "Get one at: https://platform.openai.com/api-keys"
                Write-Host ""
                $script:OPENAI_API_KEY = Read-SecurePrompt "Enter OpenAI API key"
                if (-not $script:OPENAI_API_KEY) {
                    Write-Warn "No API key provided, disabling vector memory"
                    $script:MEMORY_SEARCH_ENABLED = "false"
                    return
                }
            }
            $script:MEMORY_SEARCH_ENABLED = "true"
            $script:MEMORY_SEARCH_PROVIDER = "openai"
            $script:MEMORY_SEARCH_MODEL = "text-embedding-3-small"
            $script:MEMORY_SEARCH_API_KEY = $script:OPENAI_API_KEY
            Write-Success "Vector memory: OpenAI (text-embedding-3-small)"
        }
        default {
            Write-Warn "Invalid selection, using OpenClaw local embeddings"
            $script:MEMORY_SEARCH_ENABLED = "true"
            $script:MEMORY_SEARCH_PROVIDER = "openclaw"
            $script:MEMORY_SEARCH_MODEL = "local"
        }
    }
}

# ============================================================
# Initialize Bot Repository
# ============================================================
function Initialize-BotRepo {
    Write-Info "Initializing bot repository..."

    $botDir = Join-Path $BotReposDir $script:INSTANCE_NAME
    $approvedDir = Join-Path $botDir "approved"
    $stateDir = Join-Path $botDir "state"

    # Create directories
    if (-not (Test-Path $botDir)) {
        New-Item -ItemType Directory -Path $botDir -Force | Out-Null
    }
    if (-not (Test-Path $stateDir)) {
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    }

    # Check if approved/ already exists with OpenClaw source
    $hasDockerfile = Test-Path (Join-Path $approvedDir "Dockerfile")
    $hasPackageJson = Test-Path (Join-Path $approvedDir "package.json")

    if ($hasDockerfile -or $hasPackageJson) {
        Write-Success "Using existing bot_repos\$script:INSTANCE_NAME\approved (OpenClaw source found)"

        # Initialize git if not already
        if (-not (Test-Path (Join-Path $approvedDir ".git"))) {
            Write-Info "Initializing git repo..."
            Push-Location $approvedDir
            git init
            git add -A
            git commit -m "Initial commit from existing source" 2>$null
            Pop-Location
        }
    } else {
        # No existing source - check GitHub
        $repoOwner = $script:GITHUB_REPO_OWNER
        if (-not $repoOwner) { $repoOwner = $script:GITHUB_ORG }
        if (-not $repoOwner) { $repoOwner = $script:GITHUB_USERNAME }
        if ((-not $repoOwner) -and $script:GH_AVAILABLE) {
            try {
                $repoOwner = gh api user --jq '.login' 2>$null
            } catch { }
        }

        if (-not $repoOwner) {
            Write-Warn "No existing source in bot_repos\$script:INSTANCE_NAME\approved\"
            Write-Host ""
            Write-Host "To set up OpenClaw source, clone OpenClaw manually:"
            Write-Host ""
            Write-Host "  git clone https://github.com/openclaw/openclaw.git bot_repos\$script:INSTANCE_NAME\approved"
            Write-Host ""
            Write-Host "Or copy from an existing bot:"
            Write-Host ""
            Write-Host "  Copy-Item -Recurse bot_repos\existing_bot\approved bot_repos\$script:INSTANCE_NAME\approved"
            Write-Host ""
            if (-not $script:GH_AVAILABLE) {
                Write-Host "To enable GitHub forking, install and authenticate GitHub CLI:"
                Write-Host "  1. Install: https://cli.github.com/"
                Write-Host "  2. Authenticate: gh auth login"
                Write-Host ""
            }
            return
        }

        $botRepoName = "$script:INSTANCE_NAME-bot"
        $forkRepo = "$repoOwner/$botRepoName"
        $forkUrl = "https://github.com/$forkRepo.git"

        # Check if fork exists
        $forkExists = $false
        try {
            $null = gh repo view $forkRepo 2>$null
            $forkExists = ($LASTEXITCODE -eq 0)
        } catch { }

        if (-not $forkExists) {
            Write-Info "Forking openclaw/openclaw as $botRepoName under $repoOwner..."
            if ($script:GITHUB_ORG -and $script:GITHUB_ORG -eq $repoOwner) {
                gh repo fork openclaw/openclaw --clone=false --fork-name $botRepoName --org $script:GITHUB_ORG
            } else {
                gh repo fork openclaw/openclaw --clone=false --fork-name $botRepoName
            }
            Write-Success "Created fork: $forkRepo"
        } else {
            Write-Success "Fork exists: $forkRepo"
        }

        # Clone if needed
        if (-not (Test-Path (Join-Path $approvedDir ".git"))) {
            Write-Info "Cloning fork to approved..."
            git clone $forkUrl $approvedDir
            Write-Success "Cloned to bot_repos\$script:INSTANCE_NAME\approved"
        } else {
            Write-Success "approved already exists"
        }
    }

    # Create workspace config files if needed
    $workspaceDir = Join-Path $approvedDir "workspace"
    $soulFile = Join-Path $workspaceDir "SOUL.md"

    if (-not (Test-Path $soulFile)) {
        Write-Info "Creating bot config files..."

        New-Item -ItemType Directory -Path (Join-Path $workspaceDir "skills") -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $workspaceDir "memory") -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $workspaceDir "$($script:INSTANCE_NAME)_save") -Force | Out-Null

        # Create SOUL.md
        $soulContent = @"
# Soul

You are a helpful AI assistant running in the ClawFactory secure environment.

## Principles

1. Be helpful and honest
2. Respect user privacy
3. Admit when you don't know something
4. Follow the policies defined in your config
5. Use the proposal workflow for configuration changes

## Workspace Security

Your workspace is version-controlled. To modify your configuration:
1. Write changes to `/workspace/approved/workspace/`
2. Create a branch, commit, and push
3. Open a PR for review
4. Wait for approval before changes take effect

See `skills/propose.md` for detailed instructions.
"@
        Set-Content -Path $soulFile -Value $soulContent

        # Create policies.yml
        $policiesContent = @"
# Policies

allowed_actions:
  - read_files
  - write_proposals
  - create_commits
  - open_pull_requests
  - backup_memory
  - create_snapshots

forbidden_actions:
  - direct_promotion
  - secret_access
  - network_escalation
  - docker_access
"@
        Set-Content -Path (Join-Path $workspaceDir "policies.yml") -Value $policiesContent

        Write-Success "Bot config files created"
    }
}

# ============================================================
# Generate Tokens and Write Env Files
# ============================================================
function Write-EnvFiles {
    Write-Info "Configuring API tokens for instance '$script:INSTANCE_NAME'..."

    $tokenFile = Join-Path $SecretsDir "tokens.env"
    $instanceSecretsDir = Join-Path $SecretsDir $script:INSTANCE_NAME

    # Create instance secrets directory
    if (-not (Test-Path $instanceSecretsDir)) {
        New-Item -ItemType Directory -Path $instanceSecretsDir -Force | Out-Null
    }

    # Load or generate tokens
    $gatewayTokenVar = "$($script:INSTANCE_NAME)_gateway_token"
    $controllerTokenVar = "$($script:INSTANCE_NAME)_controller_token"

    $gatewayToken = ""
    $controllerToken = ""

    if (Test-Path $tokenFile) {
        $tokenContent = Get-Content $tokenFile
        foreach ($line in $tokenContent) {
            if ($line -match "^$gatewayTokenVar=(.*)$") {
                $gatewayToken = $Matches[1]
            }
            if ($line -match "^$controllerTokenVar=(.*)$") {
                $controllerToken = $Matches[1]
            }
        }
    }

    if (-not $gatewayToken) {
        $gatewayToken = Get-RandomHex 32
        Write-Success "Generated new gateway token for $script:INSTANCE_NAME"
    } else {
        Write-Success "Using existing gateway token for $script:INSTANCE_NAME"
    }

    if (-not $controllerToken) {
        $controllerToken = Get-RandomHex 32
        Write-Success "Generated new controller token for $script:INSTANCE_NAME"
    } else {
        Write-Success "Using existing controller token for $script:INSTANCE_NAME"
    }

    # Save tokens
    $tokenHeader = @"
# ClawFactory API Tokens
# Generated tokens for each instance (do not edit manually)
# Format: {instance}_gateway_token, {instance}_controller_token
"@

    $existingTokens = @()
    if (Test-Path $tokenFile) {
        $existingTokens = Get-Content $tokenFile | Where-Object {
            $_ -notmatch "^#" -and $_ -notmatch "^$($script:INSTANCE_NAME)_" -and $_.Trim() -ne ""
        }
    }

    $tokenContent = @($tokenHeader) + $existingTokens + @(
        "$($script:INSTANCE_NAME)_gateway_token=$gatewayToken",
        "$($script:INSTANCE_NAME)_controller_token=$controllerToken"
    )
    Set-Content -Path $tokenFile -Value ($tokenContent -join "`n")

    # Build gateway.env
    $gatewayEnvLines = @("# Gateway environment for instance: $script:INSTANCE_NAME", "", "# Chat Channels")

    if ($script:DISCORD_BOT_TOKEN) { $gatewayEnvLines += "DISCORD_BOT_TOKEN=$script:DISCORD_BOT_TOKEN" }
    if ($script:TELEGRAM_BOT_TOKEN) { $gatewayEnvLines += "TELEGRAM_BOT_TOKEN=$script:TELEGRAM_BOT_TOKEN" }
    if ($script:SLACK_BOT_TOKEN) { $gatewayEnvLines += "SLACK_BOT_TOKEN=$script:SLACK_BOT_TOKEN" }
    if ($script:SLACK_APP_TOKEN) { $gatewayEnvLines += "SLACK_APP_TOKEN=$script:SLACK_APP_TOKEN" }

    $gatewayEnvLines += @("", "# AI Providers")

    if ($script:ANTHROPIC_API_KEY) { $gatewayEnvLines += "ANTHROPIC_API_KEY=$script:ANTHROPIC_API_KEY" }
    if ($script:MOONSHOT_API_KEY) { $gatewayEnvLines += "MOONSHOT_API_KEY=$script:MOONSHOT_API_KEY" }
    if ($script:OPENAI_API_KEY) { $gatewayEnvLines += "OPENAI_API_KEY=$script:OPENAI_API_KEY" }
    if ($script:GEMINI_API_KEY) { $gatewayEnvLines += "GEMINI_API_KEY=$script:GEMINI_API_KEY" }
    if ($script:OPENROUTER_API_KEY) { $gatewayEnvLines += "OPENROUTER_API_KEY=$script:OPENROUTER_API_KEY" }
    if ($script:BRAVE_API_KEY) { $gatewayEnvLines += "BRAVE_API_KEY=$script:BRAVE_API_KEY" }

    if ($script:OLLAMA_ENABLED) {
        $gatewayEnvLines += @("", "# Ollama (local LLM)", "OLLAMA_API_KEY=ollama-local", "OLLAMA_BASE_URL=http://host.docker.internal:11434/v1")
    }

    $gatewayEnvLines += @("", "# Gateway API token", "OPENCLAW_GATEWAY_TOKEN=$gatewayToken")

    Set-Content -Path (Join-Path $instanceSecretsDir "gateway.env") -Value ($gatewayEnvLines -join "`n")

    # Build controller.env
    if (-not $script:GITHUB_WEBHOOK_SECRET) {
        $script:GITHUB_WEBHOOK_SECRET = Get-RandomHex 32
    }

    $controllerEnvContent = @"
# Controller environment for instance: $script:INSTANCE_NAME
GITHUB_WEBHOOK_SECRET=$script:GITHUB_WEBHOOK_SECRET
ALLOWED_MERGE_ACTORS=$script:GITHUB_ALLOWED_ACTORS

# Controller's own API token
CONTROLLER_API_TOKEN=$controllerToken

# Gateway token (for controller to call gateway API)
OPENCLAW_GATEWAY_TOKEN=$gatewayToken
"@

    if ($script:GITHUB_TOKEN) {
        $controllerEnvContent += "`n`n# GitHub token`nGITHUB_TOKEN=$script:GITHUB_TOKEN"
    }

    Set-Content -Path (Join-Path $instanceSecretsDir "controller.env") -Value $controllerEnvContent

    Write-Success "Environment files created"
}

# ============================================================
# Create State Config (openclaw.json)
# ============================================================
function Create-StateConfig {
    $stateDir = Join-Path $BotReposDir "$script:INSTANCE_NAME\state"
    $configFile = Join-Path $stateDir "openclaw.json"

    if (-not (Test-Path $stateDir)) {
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    }

    # Build models array
    $models = @()

    if ($script:ANTHROPIC_API_KEY) {
        $models += @{
            provider = "anthropic"
            model = "claude-sonnet-4-20250514"
            label = "Claude Sonnet 4"
        }
    }

    if ($script:MOONSHOT_API_KEY) {
        $models += @{
            provider = "moonshot"
            model = "kimi-k2.5"
            label = "Kimi K2.5"
            reasoning = $false
        }
    }

    if ($script:OPENAI_API_KEY) {
        $models += @{
            provider = "openai"
            model = "gpt-4o"
            label = "GPT-4o"
        }
    }

    if ($script:OLLAMA_ENABLED) {
        $models += @{
            provider = "ollama"
            model = "llama3.2"
            label = "Llama 3.2 (Local)"
            baseURL = "http://host.docker.internal:11434/v1"
            apiKey = "ollama-local"
        }
    }

    $config = @{
        version = 1
        gateway = @{
            mode = "local"
            auth = "token"
        }
        models = $models
    }

    # Add memory config if enabled
    if ($script:MEMORY_SEARCH_ENABLED -eq "true") {
        $config.memory = @{
            enabled = $true
            provider = $script:MEMORY_SEARCH_PROVIDER
            model = $script:MEMORY_SEARCH_MODEL
        }
        if ($script:MEMORY_SEARCH_BASE_URL) {
            $config.memory.baseURL = $script:MEMORY_SEARCH_BASE_URL
        }
        if ($script:MEMORY_SEARCH_API_KEY) {
            $config.memory.apiKey = $script:MEMORY_SEARCH_API_KEY
        }
    }

    $configJson = $config | ConvertTo-Json -Depth 10
    Set-Content -Path $configFile -Value $configJson

    Write-Success "Created openclaw.json config"
}

# ============================================================
# Create Helper Script
# ============================================================
function Create-HelperScript {
    $helperScript = @'
# ClawFactory Helper Script (Windows)
# Usage: .\clawfactory.ps1 <command>

param(
    [Parameter(Position=0)]
    [string]$Command = "help",

    [Parameter(Position=1)]
    [string]$Arg1,

    [Alias("i")]
    [string]$Instance
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Load config
$ConfigFile = Join-Path $ScriptDir ".clawfactory.conf"
if (Test-Path $ConfigFile) {
    Get-Content $ConfigFile | ForEach-Object {
        if ($_ -match '^(\w+)="?([^"]*)"?$') {
            Set-Variable -Name $Matches[1] -Value $Matches[2] -Scope Script
        }
    }
}

if ($Instance) {
    $INSTANCE_NAME = $Instance
}

if (-not $INSTANCE_NAME) {
    $INSTANCE_NAME = "default"
}

$ContainerPrefix = "clawfactory-$INSTANCE_NAME"

switch ($Command) {
    "start" {
        Write-Host "Starting ClawFactory [$INSTANCE_NAME]..." -ForegroundColor Cyan
        Push-Location $ScriptDir
        docker compose up -d
        Pop-Location
        Write-Host ""
        Write-Host "[OK] Started" -ForegroundColor Green
        Write-Host "  Gateway:    http://localhost:18789"
        Write-Host "  Controller: http://localhost:8080/controller"
    }
    "stop" {
        Write-Host "Stopping ClawFactory [$INSTANCE_NAME]..." -ForegroundColor Cyan
        Push-Location $ScriptDir
        docker compose down
        Pop-Location
        Write-Host "[OK] Stopped" -ForegroundColor Green
    }
    "restart" {
        Write-Host "Restarting ClawFactory [$INSTANCE_NAME]..." -ForegroundColor Cyan
        Push-Location $ScriptDir
        docker compose restart
        Pop-Location
        Write-Host "[OK] Restarted" -ForegroundColor Green
    }
    "status" {
        docker compose ps
    }
    "logs" {
        $container = if ($Arg1) { $Arg1 } else { "gateway" }
        docker logs -f "$ContainerPrefix-$container"
    }
    "shell" {
        $container = if ($Arg1) { $Arg1 } else { "gateway" }
        docker exec -it "$ContainerPrefix-$container" /bin/bash
    }
    "controller" {
        Write-Host "Controller UI for [$INSTANCE_NAME]:"
        Write-Host "http://127.0.0.1:8080/controller"
    }
    "info" {
        Write-Host "Instance: $INSTANCE_NAME"
        Write-Host "Containers: $ContainerPrefix-{gateway,controller,proxy}"
    }
    default {
        Write-Host "ClawFactory - Agent Runtime [$INSTANCE_NAME]" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "Usage: .\clawfactory.ps1 [-Instance <name>] <command>"
        Write-Host ""
        Write-Host "Commands:"
        Write-Host "  start           Start containers"
        Write-Host "  stop            Stop all containers"
        Write-Host "  restart         Restart all containers"
        Write-Host "  status          Show container status"
        Write-Host "  logs [service]  Follow logs (gateway/proxy/controller)"
        Write-Host "  shell [service] Open shell in container"
        Write-Host "  controller      Show controller URL"
        Write-Host "  info            Show instance info"
        Write-Host ""
        Write-Host "Local access:"
        Write-Host "  Gateway:    http://localhost:18789"
        Write-Host "  Controller: http://localhost:8080/controller"
    }
}
'@

    Set-Content -Path (Join-Path $ScriptDir "clawfactory.ps1") -Value $helperScript
    Write-Success "Helper script created: clawfactory.ps1"
}

# ============================================================
# Create .gitignore
# ============================================================
function Create-GitIgnore {
    $gitignoreContent = @"
# Secrets (NEVER commit)
secrets/
*.env

# Runtime state
audit/
bot_repos/

# OS
.DS_Store
Thumbs.db

# Backups
*.backup
"@

    Set-Content -Path (Join-Path $ScriptDir ".gitignore") -Value $gitignoreContent
    Write-Success "Created .gitignore"
}

# ============================================================
# Print Summary
# ============================================================
function Print-Summary {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "  ClawFactory Setup Complete!" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Instance: $script:INSTANCE_NAME"
    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  1. Start the stack:"
    Write-Host "     .\clawfactory.ps1 start"
    Write-Host ""
    Write-Host "  2. Access the services:"
    Write-Host "     Gateway:    http://localhost:18789"
    Write-Host "     Controller: http://localhost:8080/controller"
    Write-Host ""

    if (-not (Test-Path (Join-Path $BotReposDir "$script:INSTANCE_NAME\approved\Dockerfile"))) {
        Write-Host "  3. Set up OpenClaw source (required before starting):"
        Write-Host "     git clone https://github.com/openclaw/openclaw.git bot_repos\$script:INSTANCE_NAME\approved"
        Write-Host ""
    }

    Write-Host "For help: .\clawfactory.ps1 help"
    Write-Host ""
}

# ============================================================
# Main
# ============================================================
function Main {
    Write-Host ""
    Write-Host "+=======================================+" -ForegroundColor Cyan
    Write-Host "|       ClawFactory Installer           |" -ForegroundColor Cyan
    Write-Host "+=======================================+" -ForegroundColor Cyan
    Write-Host ""

    Test-Preflight
    Configure-Secrets

    if (-not $script:USE_SAVED_SECRETS) {
        Configure-Sandbox
    }

    Initialize-BotRepo
    Write-EnvFiles
    Create-StateConfig
    Create-HelperScript
    Create-GitIgnore
    Print-Summary
}

# Run main
Main
