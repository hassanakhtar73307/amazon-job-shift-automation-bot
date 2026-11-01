param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$JobId,
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 90,
    [switch]$SkipNext,
    [switch]$StartApplication,
    [switch]$WithdrawPreviousApplication,
    [switch]$AgreeIntegrityNotice,
    [switch]$SelectShift
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$transcriptStarted = $false
try {
    $logDirectory = Join-Path $PSScriptRoot '.diagnostics'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $logName = 'browser-automation-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + $PID + '.log'
    $automationLogPath = Join-Path $logDirectory $logName
    Start-Transcript -Path $automationLogPath -Force | Out-Null
    $transcriptStarted = $true
    Write-Output ('Browser automation log: ' + $automationLogPath)
}
catch { Write-Output ('Browser automation: could not save its diagnostic log: ' + $_.Exception.Message) }
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$scope = [System.Windows.Automation.TreeScope]::Descendants
$elementType = [System.Windows.Automation.AutomationElement]
$windowCondition = New-Object System.Windows.Automation.PropertyCondition($elementType::ClassNameProperty, 'Chrome_WidgetWin_1')
$addressCondition = New-Object System.Windows.Automation.PropertyCondition($elementType::NameProperty, 'Address and search bar')
$documentCondition = New-Object System.Windows.Automation.PropertyCondition($elementType::ControlTypeProperty, [System.Windows.Automation.ControlType]::Document)
$buttonTypeCondition = New-Object System.Windows.Automation.PropertyCondition($elementType::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
$controlWalker = [System.Windows.Automation.TreeWalker]::ControlViewWalker

function Get-PageJobIds([uri]$PageUri) {
    $parts = $PageUri.Fragment -split '\?', 2
    $query = $PageUri.Query.TrimStart('?')
    if ($parts.Count -gt 1) { $query += '&' + $parts[1] }
    foreach ($match in [regex]::Matches($query, '(?:^|&)jobId=([^&]*)')) {
        [uri]::UnescapeDataString($match.Groups[1].Value.Replace('+', ' '))
    }
}

function Get-ChromeWindows {
    # Rebuilding a Chrome element with FromHandle can expose the outer website
    # document instead of the application iframe. Keep the desktop UIA tree.
    return $elementType::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $windowCondition)
}

function Get-AmazonPages {
    param([int]$WindowHandle = 0)
    $windows = Get-ChromeWindows
    foreach ($window in $windows) {
        try {
            if ($WindowHandle -ne 0 -and $window.Current.NativeWindowHandle -ne $WindowHandle) { continue }
            $windowProcess = Get-Process -Id $window.Current.ProcessId -ErrorAction SilentlyContinue
            if ($null -eq $windowProcess -or $windowProcess.ProcessName -ne 'chrome') { continue }
            $address = $window.FindFirst($scope, $addressCondition)
            if ($null -eq $address) { continue }
            $url = $address.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
            if ($url -notmatch '^https?://') { $url = 'https://' + $url }
            $pageUri = $null
            if (-not [uri]::TryCreate($url, [UriKind]::Absolute, [ref]$pageUri)) { continue }
            if ($pageUri.Scheme -ne 'https' -or $pageUri.Host -notin @('jobsatamazon.co.uk', 'www.jobsatamazon.co.uk')) { continue }
            $step = ($pageUri.Fragment -split '\?', 2)[0]
            $ids = @(Get-PageJobIds $pageUri)
            $matchesJob = $ids.Count -gt 0 -and @($ids | Where-Object { $_ -cne $JobId }).Count -eq 0
            $document = $window.FindFirst($scope, $documentCondition)
            [pscustomobject]@{
                Window = $window
                Handle = $window.Current.NativeWindowHandle
                Uri = $pageUri
                Step = $step
                MatchesJob = $matchesJob
                Document = $document
            }
        }
        catch [System.Windows.Automation.ElementNotAvailableException] { continue }
        catch [System.InvalidOperationException] { continue }
    }
}

function Get-ActionButton($Document, [string]$Name) {
    if ($null -eq $Document) { return $null }
    $nameCondition = New-Object System.Windows.Automation.PropertyCondition($elementType::NameProperty, $Name)
    $condition = New-Object System.Windows.Automation.AndCondition($buttonTypeCondition, $nameCondition)
    $buttons = @($Document.FindAll($scope, $condition) | Where-Object { $_.Current.IsEnabled })
    if ($buttons.Count -ne 1) { return $null }
    $button = $buttons[0]
    if ($button.Current.IsOffscreen) {
        $scroll = $null
        if ($button.TryGetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern, [ref]$scroll)) {
            $scroll.ScrollIntoView()
        }
    }
    if ($button.Current.IsOffscreen) { return $null }
    return $button
}

function Get-ActiveApplicationModal($Window) {
    # The warning is a separate accessibility Window outside DocumentRange text.
    $nameCondition = New-Object System.Windows.Automation.PropertyCondition($elementType::NameProperty, 'You have an active job application')
    $typeCondition = New-Object System.Windows.Automation.PropertyCondition($elementType::ControlTypeProperty, [System.Windows.Automation.ControlType]::Window)
    $condition = New-Object System.Windows.Automation.AndCondition($nameCondition, $typeCondition)
    $modals = @($Window.FindAll($scope, $condition) | Where-Object { -not $_.Current.IsOffscreen })
    if ($modals.Count -eq 0) { return $null }
    if ($modals.Count -ne 1) { throw 'More than one active-application warning is visible. No Continue button was clicked.' }
    $textCondition = New-Object System.Windows.Automation.PropertyCondition($elementType::ControlTypeProperty, [System.Windows.Automation.ControlType]::Text)
    $text = ($modals[0].FindAll($scope, $textCondition) | ForEach-Object { $_.Current.Name }) -join ' '
    if ($text -notmatch 'existing application will be withdrawn' -or $text -notmatch 'one active application') {
        throw 'The active-application warning text changed. No Continue button was clicked.'
    }
    return $modals[0]
}

function Get-CachedShiftRows($Document) {
    if ($null -eq $Document) { return }
    $condition = New-Object System.Windows.Automation.PropertyCondition($elementType::ControlTypeProperty, [System.Windows.Automation.ControlType]::ListItem)
    $cache = New-Object System.Windows.Automation.CacheRequest
    $cache.TreeScope = [System.Windows.Automation.TreeScope]::Element
    $cache.Add($elementType::NameProperty)
    $cache.Add($elementType::IsEnabledProperty)
    $cache.Push()
    try { $Document.FindAll($scope, $condition) }
    finally { $cache.Pop() }
}

function Get-WeeklyHours([string]$Label) {
    $match = [regex]::Match($Label, '^Hours\s*:\s*(?<hours>\d+(?:\.\d+)?)\s*(?:hrs?|hours?)\s+per\s+week\s*$', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if (-not $match.Success) { return $null }
    return [double]::Parse($match.Groups['hours'].Value, [System.Globalization.CultureInfo]::InvariantCulture)
}

function Test-SameAutomationElement($First, $Second) {
    return (($First.GetRuntimeId() -join ',') -ceq ($Second.GetRuntimeId() -join ','))
}

function Get-HighestHourShift($Document) {
    # Read the hours once, discard <30, and try the largest value first.
    $order = 0
    $candidates = @(foreach ($row in @(Get-CachedShiftRows $Document)) {
        $hours = Get-WeeklyHours $row.Cached.Name
        if ($row.Cached.IsEnabled -and $null -ne $hours -and $hours -ge 30) {
            [pscustomobject]@{ Row = $row; Hours = $hours; Order = $order }
        }
        $order++
    })
    foreach ($candidate in @($candidates | Sort-Object -Property @{ Expression = 'Hours'; Descending = $true }, 'Order')) {
        $card = $controlWalker.GetParent($candidate.Row)
        if ($null -eq $card -or $card.Current.ControlType -ne [System.Windows.Automation.ControlType]::List) { continue }
        $card = $controlWalker.GetParent($card)
        while ($null -ne $card -and -not (Test-SameAutomationElement $card $Document)) {
            if (-not $card.Current.IsEnabled) { break }
            $invoke = $null
            if ($card.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$invoke)) {
                return [pscustomobject]@{ Element = $card; Invoke = $invoke; Hours = $candidate.Hours }
            }
            $card = $controlWalker.GetParent($card)
        }
    }
    return $null
}

function Invoke-ShiftCard($Shift) {
    if (-not $Shift.Element.Current.IsEnabled) { throw 'The selected shift is no longer enabled.' }
    if ($Shift.Element.Current.IsOffscreen) {
        $scroll = $null
        if ($Shift.Element.TryGetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern, [ref]$scroll)) {
            $scroll.ScrollIntoView()
        }
    }
    # Chrome can keep IsOffscreen=true until its asynchronous scroll finishes.
    # Invoke the card directly; do not abort on that transient visibility flag.
    $Shift.Invoke.Invoke()
}

function Get-ShiftConfirmationButton($Document, [double]$SelectedHours) {
    $hours = @(foreach ($row in @(Get-CachedShiftRows $Document)) {
        $value = Get-WeeklyHours $row.Cached.Name
        if ($null -ne $value) { $value }
    })
    if ($hours.Count -ne 1) { return $null }
    if ($hours[0] -lt 30 -or $hours[0] -ne $SelectedHours) {
        throw 'The selected weekly hours changed. Select this job was not clicked.'
    }
    return Get-ActionButton $Document 'Select this job'
}

function Invoke-ApplicationWorkflow {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $applyClicked = $false
    $nextClicked = $false
    $startClicked = $false
    $withdrawalConfirmed = $false
    $trackedHandle = $null
    $loginMessageShown = $false
    $integrityAgreed = $false
    $selectedShift = $null
    $shiftClicked = $false
    $jobSelected = $false
    $noShiftMessageShown = $false
    $previousStep = $null

    Write-Output 'Browser automation: waiting for the job page to load.'
    while ([DateTime]::UtcNow -lt $deadline) {
        $actionTaken = $false
        $pages = @(Get-AmazonPages -WindowHandle $trackedHandle)
        if ($null -eq $trackedHandle) {
            $jobPages = @($pages | Where-Object {
                $_.MatchesJob -and $_.Uri.AbsolutePath -match '^/app/?$' -and $_.Step -eq '#/jobDetail'
            })
            if ($jobPages.Count -eq 1) {
                $trackedHandle = $jobPages[0].Handle
            }
            elseif ($jobPages.Count -eq 0) {
                # Amazon may resume the existing application before the helper
                # sees jobDetail. Ignore old offer tabs when a live step exists.
                $applicationPages = @($pages | Where-Object {
                    $_.MatchesJob -and $_.Uri.AbsolutePath -match '^/application(?:/|$)'
                })
                $activePages = @($applicationPages | Where-Object {
                    $_.Step -in @('#/pre-consent', '#/consent', '#/application-integrity-notice',
                                 '#/job-opportunities', '#/job-opportunities/job-confirmation')
                })
                $resumePages = if ($activePages.Count -gt 0) { $activePages } else { $applicationPages }
                $resumePages = @($resumePages)
                if ($resumePages.Count -eq 1) {
                    $trackedHandle = $resumePages[0].Handle
                    Write-Output ('Browser automation: resuming the matching application at ' + $resumePages[0].Step + '.')
                }
            }
        }
        foreach ($page in $pages) {
            $isLogin = $page.Uri.AbsolutePath -match '^/app/?$' -and $page.Step -eq '#/login'
            if ($isLogin -and $null -ne $trackedHandle -and $page.Handle -eq $trackedHandle) {
                if (-not $loginMessageShown) {
                    Write-Output 'Browser automation: Amazon needs you to sign in. Waiting for you to finish in Chrome.'
                    $loginMessageShown = $true
                }
                # A completed login may return to the job page and require Apply again.
                $applyClicked = $false
                continue
            }
            if (-not $page.MatchesJob) { continue }
            if ($null -ne $trackedHandle -and $page.Handle -ne $trackedHandle) { continue }
            $isJobPage = $page.Uri.AbsolutePath -match '^/app/?$' -and $page.Step -eq '#/jobDetail'
            $isApplication = $page.Uri.AbsolutePath -match '^/application(?:/|$)'
            if ($null -eq $trackedHandle) {
                # Multiple matching windows are ambiguous; do not guess.
                continue
            }
            if ($isJobPage -and -not $applyClicked) {
                $button = Get-ActionButton $page.Document 'Apply'
                if ($null -eq $button) { continue }
                $trackedHandle = $page.Handle
                $applyClicked = $true
                $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
                $actionTaken = $true
                Write-Output 'Browser automation: clicked Apply.'
                break
            }
            if (-not $isApplication) { continue }
            if ($page.Step -ne $previousStep) {
                if ($page.Step -eq '#/job-opportunities') {
                    # If an unavailable shift returns us to the list, choose again.
                    $selectedShift = $null
                    $shiftClicked = $false
                    $jobSelected = $false
                    $noShiftMessageShown = $false
                }
                $previousStep = $page.Step
            }
            if ($page.Step -eq '#/consent') {
                $modal = Get-ActiveApplicationModal $page.Window
                if ($null -ne $modal) {
                    if (-not $StartApplication -or -not $WithdrawPreviousApplication) {
                        Write-Output 'Browser automation: an active application exists. Continue in Chrome; automatic replacement is disabled.'
                        return
                    }
                    if ($withdrawalConfirmed) { continue }
                    $button = Get-ActionButton $modal 'Continue'
                    if ($null -eq $button) { continue }
                    $withdrawalConfirmed = $true
                    $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
                    $actionTaken = $true
                    Write-Output 'Browser automation: clicked Continue in the active-application warning, confirming replacement of the previous application.'
                    break
                }
                if (-not $StartApplication) {
                    Write-Output 'Browser automation: reached consent. Review this step in Chrome.'
                    return
                }
                if ($startClicked) { continue }
                $button = Get-ActionButton $page.Document 'Start Application'
                if ($null -eq $button) { continue }
                $startClicked = $true
                $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
                $actionTaken = $true
                Write-Output 'Browser automation: clicked Start Application.'
                break
            }
            if ($page.Step -eq '#/pre-consent') {
                if ($SkipNext) {
                    Write-Output 'Browser automation: application introduction is ready. Automatic Next is disabled.'
                    return
                }
                if ($nextClicked) { continue }
                $button = Get-ActionButton $page.Document 'Next'
                if ($null -eq $button) { continue }
                $trackedHandle = $page.Handle
                $nextClicked = $true
                $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
                $actionTaken = $true
                Write-Output 'Browser automation: clicked Next on the application introduction.'
                break
            }
            if ($page.Step -eq '#/application-integrity-notice') {
                if (-not $AgreeIntegrityNotice) {
                    Write-Output 'Browser automation: reached the integrity notice. Review it in Chrome.'
                    return
                }
                if ($integrityAgreed) { continue }
                $button = Get-ActionButton $page.Document 'I Agree'
                if ($null -eq $button) { continue }
                $integrityAgreed = $true
                $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
                $actionTaken = $true
                Write-Output 'Browser automation: clicked I Agree on the application integrity notice.'
                break
            }
            if ($page.Step -eq '#/job-opportunities') {
                if (-not $SelectShift) {
                    Write-Output 'Browser automation: reached shift selection. Continue in Chrome.'
                    return
                }
                if ($shiftClicked) { continue }
                $selectedShift = Get-HighestHourShift $page.Document
                if ($null -eq $selectedShift) {
                    if (-not $noShiftMessageShown) {
                        Write-Output 'Browser automation: no selectable shift has at least 30 hours per week. Waiting for availability.'
                        $listedHours = @(foreach ($row in @(Get-CachedShiftRows $page.Document)) {
                            $value = Get-WeeklyHours $row.Cached.Name
                            if ($null -ne $value) { $value }
                        })
                        Write-Output ('Browser automation: document=' + $page.Document.Current.Name + '; listed weekly hours=' + ($listedHours -join ',') + '.')
                        $noShiftMessageShown = $true
                    }
                    continue
                }
                Invoke-ShiftCard $selectedShift
                $shiftClicked = $true
                $actionTaken = $true
                Write-Output ('Browser automation: clicked the highest-hour shift: ' + $selectedShift.Hours + ' hours per week.')
                break
            }
            if ($page.Step -eq '#/job-opportunities/job-confirmation') {
                if (-not $SelectShift -or $null -eq $selectedShift) {
                    Write-Output 'Browser automation: reached job confirmation without an automatically selected shift. Continue in Chrome.'
                    return
                }
                if ($jobSelected) { continue }
                $button = Get-ShiftConfirmationButton $page.Document $selectedShift.Hours
                if ($null -eq $button) { continue }
                $jobSelected = $true
                $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
                $actionTaken = $true
                Write-Output 'Browser automation: clicked Select this job.'
                break
            }
            if ($page.Step -eq '#/contingent-offer') {
                Write-Output 'Browser automation: reached the contingent-offer page. Review the offer in Chrome.'
                return
            }
            if ($page.Step -eq '#/timeout') {
                throw 'Amazon timed out the application page. Restart the application to continue; this run did not complete shift selection.'
            }
            # The remaining application steps need the user's choices.
            if ($page.Step -notin @('', '#', '#/', '#/pre-consent')) {
                Write-Output 'Browser automation: application is beyond the configured steps. Continue in Chrome.'
                return
            }
        }
        if (-not $actionTaken) { Start-Sleep -Milliseconds 100 }
    }
    throw ('Timed out after ' + $TimeoutSeconds + ' seconds. Keep the job window open and check for a sign-in prompt, eligible shift, or missing button. Job fetching does not restart automatically.')
}

try {
    Invoke-ApplicationWorkflow
    exit 0
}
catch {
    Write-Output ('Browser automation failed: ' + $_.Exception.Message)
    exit 1
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
