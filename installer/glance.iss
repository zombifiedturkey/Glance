; Glance installer (Inno Setup 6)
; Built by .github/workflows/build.yml:  ISCC /DAppVersion=1.0.0 installer\glance.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Glance"
#define AppExe "Glance.exe"
#define PawnIOUrl "https://github.com/namazso/PawnIO.Setup/releases/latest/download/PawnIO_setup.exe"

[Setup]
AppId={{6F3C2B1E-9A4D-4E55-8B71-3C2D9E0A7F41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=zombifiedturkey
AppPublisherURL=https://github.com/zombifiedturkey/Glance
AppSupportURL=https://github.com/zombifiedturkey/Glance/issues
AppUpdatesURL=https://github.com/zombifiedturkey/Glance/releases
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=..\release
OutputBaseFilename=Glance-Setup-{#AppVersion}
SetupIconFile=..\assets\glance.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=force
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "pawnio"; Description: "Install the PawnIO driver (needed for CPU and motherboard temperatures)"; GroupDescription: "Temperatures:"; Check: not PawnIOInstalled
Name: "autostart"; Description: "Start Glance when I sign in to Windows"; GroupDescription: "Startup:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\Glance\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Same value Glance's own "Start with Windows" menu option writes
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{tmp}\PawnIO_setup.exe"; Parameters: "-install -silent"; StatusMsg: "Installing the PawnIO driver..."; Tasks: pawnio; Check: PawnIODownloaded; Flags: waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Start Glance now"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopGlance"

[Code]
var
  DownloadPage: TDownloadWizardPage;
  GotPawnIO: Boolean;

function PawnIOInstalled: Boolean;
begin
  Result := RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\PawnIO');
end;

function PawnIODownloaded: Boolean;
begin
  Result := GotPawnIO;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), 'Downloading the PawnIO driver from its official GitHub release...', nil);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and WizardIsTaskSelected('pawnio') then
  begin
    DownloadPage.Clear;
    DownloadPage.Add('{#PawnIOUrl}', 'PawnIO_setup.exe', '');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
        GotPawnIO := True;
      except
        GotPawnIO := False;
        SuppressibleMsgBox('Couldn''t download the PawnIO driver, so Glance will install without it. ' +
          'GPU and drive temperatures will still work. You can add CPU temperatures later by ' +
          'running this installer again.' + #13#10#13#10 + GetExceptionMessage,
          mbInformation, MB_OK, IDOK);
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  { Stop a running Glance (and its sensor reader) so its files can be replaced }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#AppName}');
end;
