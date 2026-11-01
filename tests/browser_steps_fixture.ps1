param([Parameter(Mandatory = $true)][string]$SourcePath)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$scope = [System.Windows.Automation.TreeScope]::Descendants
$elementType = [System.Windows.Automation.AutomationElement]
$buttonTypeCondition = New-Object System.Windows.Automation.PropertyCondition($elementType::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)

# Load only the actual selector functions; never run the browser workflow.
$parseTokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($SourcePath, [ref]$parseTokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw 'The browser helper has PowerShell syntax errors.' }
foreach ($functionName in @('Get-PageJobIds', 'Get-AmazonPages', 'Get-ActiveApplicationModal', 'Get-ActionButton', 'Get-CachedShiftRows',
                           'Get-WeeklyHours', 'Test-SameAutomationElement', 'Get-HighestHourShift', 'Invoke-ShiftCard',
                           'Get-ShiftConfirmationButton', 'Invoke-ApplicationWorkflow')) {
    $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName }, $true)
    if ($null -eq $definition) { throw ('Missing function: ' + $functionName) }
    . ([scriptblock]::Create($definition.Extent.Text))
}

function Test-FakeCondition($Element, $Condition) {
    if ($Condition -is [System.Windows.Automation.AndCondition]) {
        foreach ($part in $Condition.GetConditions()) {
            if (-not (Test-FakeCondition $Element $part)) { return $false }
        }
        return $true
    }
    if ($Condition -is [System.Windows.Automation.PropertyCondition]) {
        if ($Condition.Property -eq $elementType::NameProperty) { return $Element.Current.Name -ceq $Condition.Value }
        if ($Condition.Property -eq $elementType::ControlTypeProperty) { return $Element.Current.ControlType.Id -eq $Condition.Value }
    }
    throw 'The fixture encountered an unsupported selector.'
}

function Get-FakeDescendants($Element) {
    foreach ($child in $Element.Children) {
        $child
        Get-FakeDescendants $child
    }
}

function New-FakeElement([string]$Name, $Type, [object[]]$Children = @(), [bool]$Offscreen = $false, [bool]$Enabled = $true, [switch]$Invokable) {
    $pattern = [pscustomobject]@{ Calls = 0; Action = $null; Current = [pscustomobject]@{ Value = '' } }
    $pattern | Add-Member ScriptMethod Invoke {
        $this.Calls++
        if ($null -ne $this.Action) { & $this.Action }
    }
    $properties = [pscustomobject]@{ Name = $Name; ControlType = $Type; IsOffscreen = $Offscreen; IsEnabled = $Enabled }
    $element = [pscustomobject]@{
        Id = [guid]::NewGuid().ToString()
        Current = $properties
        Cached = $properties
        Children = $Children
        Pattern = $pattern
        Parent = $null
        Invokable = ($Invokable -or $Type -eq [System.Windows.Automation.ControlType]::Button)
        FindAllCalls = 0
        ScrollPattern = $null
    }
    foreach ($child in $Children) { $child.Parent = $element }
    $scrollPattern = [pscustomobject]@{ Element = $element; Calls = 0; MakeVisible = $true }
    $scrollPattern | Add-Member ScriptMethod ScrollIntoView {
        $this.Calls++
        if ($this.MakeVisible) { $this.Element.Current.IsOffscreen = $false }
    }
    $element.ScrollPattern = $scrollPattern
    $element | Add-Member ScriptMethod FindAll {
        param($treeScope, $condition)
        $this.FindAllCalls++
        @(Get-FakeDescendants $this | Where-Object { Test-FakeCondition $_ $condition })
    }
    $element | Add-Member ScriptMethod FindFirst {
        param($treeScope, $condition)
        $found = @($this.FindAll($treeScope, $condition))
        if ($found.Count -gt 0) { return $found[0] }
        return $null
    }
    $element | Add-Member ScriptMethod GetCurrentPattern { param($requestedPattern) $this.Pattern }
    $element | Add-Member ScriptMethod GetRuntimeId { $this.Id }
    $element | Add-Member ScriptMethod TryGetCurrentPattern {
        param($requestedPattern, [ref]$result)
        if ($requestedPattern -eq [System.Windows.Automation.InvokePattern]::Pattern -and $this.Invokable) {
            $result.Value = $this.Pattern
            return $true
        }
        if ($requestedPattern -eq [System.Windows.Automation.ScrollItemPattern]::Pattern) {
            $result.Value = $this.ScrollPattern
            return $true
        }
        return $false
    }
    return $element
}

function Assert-True([bool]$Value, [string]$Message) {
    if (-not $Value) { throw $Message }
}

function Assert-Throws([scriptblock]$Action, [string]$Message) {
    $threw = $false
    try { & $Action } catch { $threw = $true }
    Assert-True $threw $Message
}

$buttonType = [System.Windows.Automation.ControlType]::Button
$textType = [System.Windows.Automation.ControlType]::Text
$windowType = [System.Windows.Automation.ControlType]::Window
$warning = 'You can have only one active application at a time. If you submit this application, your existing application will be withdrawn.'
$insideContinue = New-FakeElement 'Continue' $buttonType
$outsideContinue = New-FakeElement 'Continue' $buttonType
$warningText = New-FakeElement $warning $textType
$modal = New-FakeElement 'You have an active job application' $windowType @($warningText, $insideContinue)
$hiddenModal = New-FakeElement 'You have an active job application' $windowType @() $true
$root = New-FakeElement 'Browser' $windowType @($outsideContinue, $hiddenModal, $modal)
$selectedModal = Get-ActiveApplicationModal $root
Assert-True ($selectedModal.Id -eq $modal.Id) 'The visible warning modal was not selected.'
$selectedButton = Get-ActionButton $selectedModal 'Continue'
Assert-True ($selectedButton.Id -eq $insideContinue.Id) 'Continue was selected outside the warning modal.'
$selectedButton.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
Assert-True ($insideContinue.Pattern.Calls -eq 1 -and $outsideContinue.Pattern.Calls -eq 0) 'An unrelated Continue button was invoked.'

$unrelated = New-FakeElement 'Unrelated dialog' $windowType @($warningText, $outsideContinue)
$unrelatedRoot = New-FakeElement 'Browser' $windowType @($hiddenModal, $unrelated)
Assert-True ($null -eq (Get-ActiveApplicationModal $unrelatedRoot)) 'A hidden or unrelated modal was accepted.'

$changedWarning = New-FakeElement 'This is a different confirmation.' $textType
$changedModal = New-FakeElement 'You have an active job application' $windowType @($changedWarning, $insideContinue)
$changedRoot = New-FakeElement 'Browser' $windowType @($changedModal)
Assert-Throws { Get-ActiveApplicationModal $changedRoot } 'A warning with changed consequences was accepted.'

$duplicateRoot = New-FakeElement 'Browser' $windowType @($modal, $modal)
Assert-Throws { Get-ActiveApplicationModal $duplicateRoot } 'Ambiguous warning modals were accepted.'

$disabledContinue = New-FakeElement 'Continue' $buttonType @() $false $false
$disabledModal = New-FakeElement 'You have an active job application' $windowType @($warningText, $disabledContinue)
Assert-True ($null -eq (Get-ActionButton $disabledModal 'Continue')) 'A disabled Continue button was accepted.'

$duplicateButtons = New-FakeElement 'You have an active job application' $windowType @($warningText, $insideContinue, $outsideContinue)
Assert-True ($null -eq (Get-ActionButton $duplicateButtons 'Continue')) 'Ambiguous Continue buttons were accepted.'
Write-Output 'Passed modal selection, withdrawal text, unrelated button, hidden modal, disabled button, and ambiguity checks using fake accessibility elements.'

$groupType = [System.Windows.Automation.ControlType]::Group
$listType = [System.Windows.Automation.ControlType]::List
$listItemType = [System.Windows.Automation.ControlType]::ListItem
$documentType = [System.Windows.Automation.ControlType]::Document
$controlWalker = [pscustomobject]@{}
$controlWalker | Add-Member ScriptMethod GetParent { param($element) $element.Parent }

function New-FakeShift([string]$Hours, [string]$Schedule, [bool]$Enabled = $true, [bool]$Offscreen = $false) {
    $rows = @(
        (New-FakeElement 'Start Date:Saturday, 7 Nov 2026' $listItemType),
        (New-FakeElement 'End Date:Saturday, 26 Dec 2026' $listItemType),
        (New-FakeElement ('Shift:' + $Schedule) $listItemType),
        (New-FakeElement 'Pay rate:15.10/hr' $listItemType),
        (New-FakeElement ('Hours:' + $Hours + ' per week') $listItemType),
        (New-FakeElement 'Location:Trentham Lakes' $listItemType)
    )
    $list = New-FakeElement '' $listType $rows
    return New-FakeElement '' $groupType @($list) $Offscreen $Enabled -Invokable
}

# The user's example must choose 40, even though three earlier cards offer 30.
$exampleCards = @(
    (New-FakeShift '30hrs' 'first'), (New-FakeShift '30hrs' 'second'),
    (New-FakeShift '30hrs' 'third'), (New-FakeShift '40hrs' 'maximum'),
    (New-FakeShift '20hrs' 'below threshold')
)
$exampleDocument = New-FakeElement 'Shifts' $documentType $exampleCards
$chosen = Get-HighestHourShift $exampleDocument
Assert-True ($chosen.Hours -eq 40 -and $chosen.Element.Id -eq $exampleCards[3].Id) 'The example did not choose the 40-hour shift.'
Assert-True ($exampleDocument.FindAllCalls -eq 1) 'The listed hours were not collected in a single query.'
Invoke-ShiftCard $chosen
foreach ($index in 0..4) {
    Assert-True ($exampleCards[$index].Pattern.Calls -eq [int]($index -eq 3)) 'A lower-hour shift was clicked.'
}

$decimalCard = New-FakeShift '40.5 hours' 'decimal'
$integerCard = New-FakeShift '40hrs' 'integer'
$decimalDocument = New-FakeElement 'Shifts' $documentType @($integerCard, $decimalCard)
Assert-True ((Get-HighestHourShift $decimalDocument).Element.Id -eq $decimalCard.Id) 'Decimal weekly hours were not compared numerically.'

$boundaryCard = New-FakeShift '30hrs' 'boundary'
$lowCard = New-FakeShift '29.99hrs' 'under boundary'
$exactBoundaryDocument = New-FakeElement 'Shifts' $documentType @($lowCard, $boundaryCard)
Assert-True ((Get-HighestHourShift $exactBoundaryDocument).Element.Id -eq $boundaryCard.Id) 'Exactly 30 hours must qualify.'
$justAboveCard = New-FakeShift '30.01hrs' 'just above boundary'
$boundaryDocument = New-FakeElement 'Shifts' $documentType @($lowCard, $boundaryCard, $justAboveCard)
Assert-True ((Get-HighestHourShift $boundaryDocument).Element.Id -eq $justAboveCard.Id) 'A higher-hours shift must take priority over exactly 30.'
$noMatchDocument = New-FakeElement 'Shifts' $documentType @((New-FakeShift '20hrs' 'low'), (New-FakeShift '29.99hrs' 'low decimal'))
Assert-True ($null -eq (Get-HighestHourShift $noMatchDocument)) 'A shift below 30 hours was selected.'
$emptyDocument = New-FakeElement 'Shifts' $documentType
Assert-True ($null -eq (Get-HighestHourShift $emptyDocument)) 'An empty list produced a selection.'

$tieFirst = New-FakeShift '40hrs' 'first tie'
$tieSecond = New-FakeShift '40hrs' 'second tie'
$tieDocument = New-FakeElement 'Shifts' $documentType @($tieFirst, $tieSecond)
Assert-True ((Get-HighestHourShift $tieDocument).Element.Id -eq $tieFirst.Id) 'Equal maximum hours must choose the first listed shift.'

$disabledCard = New-FakeShift '50hrs' 'disabled' $false
$availableCard = New-FakeShift '40hrs' 'available'
$disabledDocument = New-FakeElement 'Shifts' $documentType @($disabledCard, $availableCard)
Assert-True ((Get-HighestHourShift $disabledDocument).Element.Id -eq $availableCard.Id) 'A disabled card was selected.'

$belowFoldCard = New-FakeShift '50hrs' 'below fold' $true $true
$visibleCard = New-FakeShift '30hrs' 'visible'
$scrollDocument = New-FakeElement 'Shifts' $documentType @($visibleCard, $belowFoldCard)
$offscreenChoice = Get-HighestHourShift $scrollDocument
Invoke-ShiftCard $offscreenChoice
Assert-True ($offscreenChoice.Element.Id -eq $belowFoldCard.Id -and $belowFoldCard.ScrollPattern.Calls -eq 1 -and $belowFoldCard.Pattern.Calls -eq 1) 'The highest-hour card below the fold was not scrolled into view and selected.'

$asyncScrollCard = New-FakeShift '40hrs' 'asynchronous scroll' $true $true
$asyncScrollCard.ScrollPattern.MakeVisible = $false
$asyncScrollDocument = New-FakeElement 'Shifts' $documentType @($asyncScrollCard)
Invoke-ShiftCard (Get-HighestHourShift $asyncScrollDocument)
Assert-True ($asyncScrollCard.Current.IsOffscreen -and $asyncScrollCard.Pattern.Calls -eq 1) 'A pending Chrome scroll prevented the shift click.'
Write-Output 'Passed clicking a shift while Chrome still reports its asynchronous scroll as offscreen.'

$malformedCard = New-FakeShift '30-40hrs' 'range'
$unrelatedHours = New-FakeElement 'Hours:80hrs per week' $listItemType
$malformedDocument = New-FakeElement 'Shifts' $documentType @($malformedCard, $unrelatedHours, (New-FakeShift '35hrs' 'valid'))
Assert-True ((Get-HighestHourShift $malformedDocument).Hours -eq 35) 'An ambiguous range or unrelated hours label was selected.'

# Hours alone decide the selection; other shift details are not required.
$minimalHours = New-FakeElement 'Hours:45hrs per week' $listItemType
$minimalList = New-FakeElement '' $listType @($minimalHours)
$minimalCard = New-FakeElement '' $groupType @($minimalList) -Invokable
$minimalDocument = New-FakeElement 'Shifts' $documentType @((New-FakeShift '40hrs' 'lower'), $minimalCard)
Assert-True ((Get-HighestHourShift $minimalDocument).Element.Id -eq $minimalCard.Id) 'Selection still depends on date, schedule, pay, or location metadata.'

$confirmButton = New-FakeElement 'Select this job' $buttonType
$confirmationCard = New-FakeShift '40hrs' 'maximum'
$confirmationDocument = New-FakeElement 'Confirmation' $documentType @($confirmationCard, $confirmButton)
Assert-True ((Get-ShiftConfirmationButton $confirmationDocument $chosen.Hours).Id -eq $confirmButton.Id) 'The matching shift confirmation was rejected.'
$wrongHoursDocument = New-FakeElement 'Confirmation' $documentType @((New-FakeShift '30hrs' 'maximum'), $confirmButton)
Assert-Throws { Get-ShiftConfirmationButton $wrongHoursDocument $chosen.Hours } 'A changed hours value was confirmed.'
Assert-True ((Get-ShiftConfirmationButton $wrongHoursDocument 30).Id -eq $confirmButton.Id) 'Exactly 30 hours must also pass job confirmation.'
$wrongScheduleDocument = New-FakeElement 'Confirmation' $documentType @((New-FakeShift '40hrs' 'different schedule'), $confirmButton)
Assert-True ((Get-ShiftConfirmationButton $wrongScheduleDocument $chosen.Hours).Id -eq $confirmButton.Id) 'Confirmation should compare weekly hours only.'
Assert-True ($confirmButton.Pattern.Calls -eq 0) 'Confirmation validation unexpectedly clicked a button.'
Write-Output 'Passed maximum-hour selection, inclusive 30-hour boundary, decimals, ties, disabled cards, offscreen cards, malformed hours, and hours-only confirmation.'

# Real page discovery must retain the window from the desktop UIA tree on
# subsequent polls; rebuilding it from a native handle hid the shift iframe.
$JobId = 'FIXTURE-JOB'
$addressCondition = New-Object System.Windows.Automation.PropertyCondition($elementType::NameProperty, 'Address and search bar')
$documentCondition = New-Object System.Windows.Automation.PropertyCondition($elementType::ControlTypeProperty, $documentType)
$address = New-FakeElement 'Address and search bar' ([System.Windows.Automation.ControlType]::Edit)
$address.Pattern.Current.Value = 'https://www.jobsatamazon.co.uk/application/uk/?jobId=FIXTURE-JOB#/job-opportunities?jobId=FIXTURE-JOB'
$desktopWindow = New-FakeElement 'Chrome' $windowType @($address, $exampleDocument)
$desktopWindow.Current | Add-Member NoteProperty NativeWindowHandle 101
$desktopWindow.Current | Add-Member NoteProperty ProcessId 123
$otherAddress = New-FakeElement 'Address and search bar' ([System.Windows.Automation.ControlType]::Edit)
$otherAddress.Pattern.Current.Value = 'https://www.jobsatamazon.co.uk/application/uk/?jobId=OTHER-JOB#/job-opportunities?jobId=OTHER-JOB'
$otherWindow = New-FakeElement 'Other Chrome' $windowType @($otherAddress, (New-FakeElement 'Other document' $documentType))
$otherWindow.Current | Add-Member NoteProperty NativeWindowHandle 202
$otherWindow.Current | Add-Member NoteProperty ProcessId 124
$script:desktopReads = 0
function Get-ChromeWindows {
    $script:desktopReads++
    return @($otherWindow, $desktopWindow)
}
function Get-Process {
    param([int]$Id, $ErrorAction)
    return [pscustomobject]@{ ProcessName = 'chrome' }
}
$discoveredPages = @(Get-AmazonPages | Where-Object { $_.MatchesJob })
$trackedPages = @(Get-AmazonPages -WindowHandle 101)
Assert-True ($discoveredPages.Count -eq 1 -and $trackedPages.Count -eq 1) 'Job matching or tracked-window filtering failed.'
Assert-True ($script:desktopReads -eq 2) 'Tracked-window polling bypassed the desktop accessibility tree.'
Assert-True ((Test-SameAutomationElement $discoveredPages[0].Document $trackedPages[0].Document)) 'Tracking switched to a different document provider.'
Assert-True ((Get-HighestHourShift $trackedPages[0].Document).Hours -eq 40) 'Shift cards disappeared after tracking the window.'
Write-Output 'Passed desktop accessibility document preservation after tracking the Chrome window.'

# Exercise the actual saved workflow with fake pages and navigation callbacks.
function New-WorkflowPage([string]$Step, $Document, $Modal = $null) {
    $path = if ($Step -eq '#/jobDetail') { '/app' } else { '/application/uk/' }
    $window = New-FakeElement 'Chrome' $windowType @($Document)
    if ($null -ne $Modal) { $window = New-FakeElement 'Chrome' $windowType @($Document, $Modal) }
    return [pscustomobject]@{ Uri = [uri]('https://www.jobsatamazon.co.uk' + $path); Step = $Step; MatchesJob = $true; Handle = 101; Document = $Document; Window = $window }
}

$applyButton = New-FakeElement 'Apply' $buttonType
$nextButton = New-FakeElement 'Next' $buttonType
$startButton = New-FakeElement 'Start Application' $buttonType
$continueButton = New-FakeElement 'Continue' $buttonType
$agreeButton = New-FakeElement 'I Agree' $buttonType
$selectJobButton = New-FakeElement 'Select this job' $buttonType
$acceptOfferButton = New-FakeElement 'I Accept' $buttonType
$workflowCards = @((New-FakeShift '30hrs' 'first'), (New-FakeShift '30hrs' 'second'), (New-FakeShift '30hrs' 'third'), (New-FakeShift '40hrs' 'maximum'), (New-FakeShift '20hrs' 'low'))
$workflowCards[3].Current.IsOffscreen = $true
$workflowCards[3].ScrollPattern.MakeVisible = $false
$workflowModal = New-FakeElement 'You have an active job application' $windowType @((New-FakeElement $warning $textType), $continueButton)
$script:workflowPages = @{
    job = (New-WorkflowPage '#/jobDetail' (New-FakeElement 'Job' $documentType @($applyButton)))
    intro = (New-WorkflowPage '#/pre-consent' (New-FakeElement 'Introduction' $documentType @($nextButton)))
    consent = (New-WorkflowPage '#/consent' (New-FakeElement 'Consent' $documentType @($startButton)))
    modal = (New-WorkflowPage '#/consent' (New-FakeElement 'Consent' $documentType @($startButton)) $workflowModal)
    integrity = (New-WorkflowPage '#/application-integrity-notice' (New-FakeElement 'Integrity' $documentType @($agreeButton)))
    shifts = (New-WorkflowPage '#/job-opportunities' (New-FakeElement 'Shifts' $documentType $workflowCards))
    confirmation = (New-WorkflowPage '#/job-opportunities/job-confirmation' (New-FakeElement 'Confirmation' $documentType @((New-FakeShift '40hrs' 'maximum'), $selectJobButton)))
    offer = (New-WorkflowPage '#/contingent-offer' (New-FakeElement 'Offer' $documentType @($acceptOfferButton)))
}
$applyButton.Pattern.Action = { $script:workflowPage = $script:workflowPages.intro }
$nextButton.Pattern.Action = { $script:workflowPage = $script:workflowPages.consent }
$startButton.Pattern.Action = { $script:workflowPage = $script:workflowPages.modal }
$continueButton.Pattern.Action = { $script:workflowPage = $script:workflowPages.integrity }
$agreeButton.Pattern.Action = { $script:workflowPage = $script:workflowPages.shifts }
$workflowCards[3].Pattern.Action = { $script:workflowPage = $script:workflowPages.confirmation }
$selectJobButton.Pattern.Action = { $script:workflowPage = $script:workflowPages.offer }

function Get-AmazonPages {
    param([int]$WindowHandle = 0)
    $script:pageReads++
    if ($script:pageReads -gt 15) { throw 'The workflow stopped advancing.' }
    if ($script:pageReads -gt 1 -and $WindowHandle -ne 101) { throw 'The workflow did not reuse the tracked Chrome window.' }
    return @(@($script:extraPages) + @($script:workflowPage) | Where-Object { $WindowHandle -eq 0 -or $_.Handle -eq $WindowHandle })
}
function Start-Sleep {
    param([int]$Milliseconds)
    if ($Milliseconds -ne 100) { throw 'Unexpected browser polling delay.' }
    $script:idleWaits++
}
function Reset-FakeWorkflow {
    $script:workflowPage = $script:workflowPages.job
    $script:pageReads = 0
    $script:idleWaits = 0
    $script:extraPages = @()
    foreach ($element in @($applyButton, $nextButton, $startButton, $continueButton, $agreeButton, $selectJobButton, $acceptOfferButton) + $workflowCards) {
        $element.Pattern.Calls = 0
    }
}
$TimeoutSeconds = 5
$SkipNext = $false
$StartApplication = $true
$WithdrawPreviousApplication = $true
$AgreeIntegrityNotice = $true
$SelectShift = $true
Reset-FakeWorkflow
$workflowOutput = @(Invoke-ApplicationWorkflow)
foreach ($button in @($applyButton, $nextButton, $startButton, $continueButton, $agreeButton, $selectJobButton)) {
    Assert-True ($button.Pattern.Calls -eq 1) ('The saved workflow did not click exactly once: ' + $button.Current.Name)
}
Assert-True ($workflowCards[3].Pattern.Calls -eq 1) 'The saved workflow did not choose the 40-hour shift.'
foreach ($index in @(0, 1, 2, 4)) { Assert-True ($workflowCards[$index].Pattern.Calls -eq 0) 'The saved workflow clicked a lower-hour shift.' }
Assert-True ($acceptOfferButton.Pattern.Calls -eq 0) 'The workflow went past the contingent-offer page.'
Assert-True ($script:pageReads -eq 8 -and $script:idleWaits -eq 0) 'The workflow added unnecessary waits between ready steps.'
Assert-True (($workflowOutput -join ' ') -match 'reached the contingent-offer page') 'The workflow did not report completion.'

# A restarted application can bypass jobDetail entirely. An older completed
# offer window must not hide the active shift list for the same job.
Reset-FakeWorkflow
$script:workflowPage = $script:workflowPages.shifts
$oldOffer = New-WorkflowPage '#/contingent-offer' (New-FakeElement 'Old offer' $documentType)
$oldOffer.Handle = 202
$script:extraPages = @($oldOffer)
$resumedOutput = @(Invoke-ApplicationWorkflow)
Assert-True ($workflowCards[3].Pattern.Calls -eq 1 -and $selectJobButton.Pattern.Calls -eq 1) 'A restart directly at shift selection did not finish selecting the highest-hour shift.'
Assert-True ($applyButton.Pattern.Calls -eq 0 -and $acceptOfferButton.Pattern.Calls -eq 0) 'Resuming repeated Apply or accepted an offer.'
Assert-True ($script:pageReads -eq 3 -and $script:idleWaits -eq 0) 'Resuming the shift page added waits or extra steps.'
Assert-True (($resumedOutput -join ' ') -match 'resuming the matching application at #/job-opportunities') 'The direct application resume was not reported.'

Reset-FakeWorkflow
$AgreeIntegrityNotice = $false
$null = @(Invoke-ApplicationWorkflow)
Assert-True ($agreeButton.Pattern.Calls -eq 0 -and $selectJobButton.Pattern.Calls -eq 0) 'The integrity agreement disable setting was ignored.'

Reset-FakeWorkflow
$AgreeIntegrityNotice = $true
$SelectShift = $false
$null = @(Invoke-ApplicationWorkflow)
Assert-True ($workflowCards[3].Pattern.Calls -eq 0 -and $selectJobButton.Pattern.Calls -eq 0) 'The shift selection disable setting was ignored.'

Reset-FakeWorkflow
$script:workflowPage = New-WorkflowPage '#/timeout' (New-FakeElement 'Timed out' $documentType)
Assert-Throws { Invoke-ApplicationWorkflow } 'A timed-out application was incorrectly reported as completed.'
Write-Output 'Passed the saved Apply, Next, Start Application, modal Continue, I Agree, maximum-hour shift, Select this job, and contingent-offer workflow without live browser actions or artificial transition waits.'
Write-Output 'Passed restarting directly at shift selection with an older offer window still open.'
