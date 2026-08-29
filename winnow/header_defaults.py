"""Default header-set nicknames for common forensic tool output.

The "database of headers" idea (see workspace.HeaderNicknames) only pays off
once the common header sets have names — and the common ones are knowable in
advance: EZ Tools/KAPE output shapes are stable enough that an analyst's
Nth case starts with the same EvtxECmd/MFTECmd/Amcache headers as their
first. So Winnow ships names for them, seeded once into the analyst's
workspace (HeaderNicknames.ensure_seeded) where they become ordinary
records: rename or delete freely, nothing here overrides a choice you've
made. Bumping DEFAULTS_VERSION after adding entries re-seeds only header
sets not already present — though a *deleted* default whose set arrives in
a later version's list can return; acceptable for data this static.

Every set below except the last three was extracted from real tool output
(a KAPE triage: EvtxECmd, MFTECmd, Amcache, PECmd, LECmd, JLECmd, SBECmd,
WxTCmd, RBCmd, AppCompatCache — 2026-08 EZ Tools versions). Column order
here is cosmetic; matching is by HeaderNicknames' key (sorted, lowercased),
so a tool release that only reorders columns still matches, while one that
adds or renames a column will not — add the new shape as a new entry rather
than editing the old one, since files produced by the older release are
still out there.

Data only. Imports nothing from the app; nothing here executes.
"""

DEFAULTS_VERSION = 1

# (nickname, columns as the tool writes them)
DEFAULT_HEADER_NICKNAMES = [
    (
        'Event logs (EvtxECmd)',
        ['RecordNumber', 'EventRecordId', 'TimeCreated', 'EventId', 'Level', 'Provider', 'Channel', 'ProcessId', 'ThreadId', 'Computer', 'ChunkNumber', 'UserId', 'MapDescription', 'UserName', 'RemoteHost', 'PayloadData1', 'PayloadData2', 'PayloadData3', 'PayloadData4', 'PayloadData5', 'PayloadData6', 'ExecutableInfo', 'HiddenRecord', 'SourceFile', 'Keywords', 'ExtraDataOffset', 'Payload'],
    ),
    (
        'Amcache — file entries',
        ['ApplicationName', 'ProgramId', 'FileKeyLastWriteTimestamp', 'SHA1', 'IsOsComponent', 'FullPath', 'Name', 'FileExtension', 'LinkDate', 'ProductName', 'Size', 'Version', 'ProductVersion', 'LongPathHash', 'BinaryType', 'IsPeFile', 'BinFileVersion', 'BinProductVersion', 'Usn', 'Language', 'Description'],
    ),
    (
        'Amcache — device containers',
        ['KeyName', 'KeyLastWriteTimestamp', 'Categories', 'DiscoveryMethod', 'FriendlyName', 'Icon', 'IsActive', 'IsConnected', 'IsMachineContainer', 'IsNetworked', 'IsPaired', 'Manufacturer', 'ModelId', 'ModelName', 'ModelNumber', 'PrimaryCategory', 'State'],
    ),
    (
        'Amcache — device PnPs',
        ['KeyName', 'KeyLastWriteTimestamp', 'BusReportedDescription', 'Class', 'ClassGuid', 'Compid', 'ContainerId', 'Description', 'DriverId', 'DriverPackageStrongName', 'DriverName', 'DriverVerDate', 'DriverVerVersion', 'Enumerator', 'HWID', 'Inf', 'InstallState', 'Manufacturer', 'MatchingId', 'Model', 'ParentId', 'ProblemCode', 'Provider', 'Service', 'Stackid'],
    ),
    (
        'Amcache — driver binaries',
        ['KeyName', 'KeyLastWriteTimestamp', 'DriverTimeStamp', 'DriverLastWriteTime', 'DriverName', 'DriverInBox', 'DriverIsKernelMode', 'DriverSigned', 'DriverCheckSum', 'DriverCompany', 'DriverId', 'DriverPackageStrongName', 'DriverType', 'DriverVersion', 'ImageSize', 'Inf', 'Product', 'ProductVersion', 'Service', 'WdfVersion'],
    ),
    (
        'Amcache — driver packages',
        ['KeyName', 'KeyLastWriteTimestamp', 'Date', 'Class', 'Directory', 'DriverInBox', 'Hwids', 'Inf', 'Provider', 'SubmissionId', 'SYSFILE', 'Version'],
    ),
    (
        'Amcache — program entries',
        ['ProgramId', 'KeyLastWriteTimestamp', 'Name', 'Version', 'Publisher', 'InstallDateArpLastModified', 'InstallDate', 'InstallDateMsi', 'OSVersionAtInstallTime', 'InstallDateFromLinkFile', 'BundleManifestPath', 'HiddenArp', 'InboxModernApp', 'Language', 'ManifestPath', 'MsiPackageCode', 'MsiProductCode', 'PackageFullName', 'ProgramInstanceId', 'RegistryKeyPath', 'RootDirPath', 'Type', 'Source', 'StoreAppType', 'UninstallString', 'Manufacturer'],
    ),
    (
        'Amcache — shortcuts',
        ['KeyName', 'LnkName', 'KeyLastWriteTimestamp'],
    ),
    (
        'Shimcache (AppCompatCache)',
        ['ControlSet', 'CacheEntryPosition', 'Path', 'LastModifiedTimeUTC', 'Executed', 'Duplicate', 'SourceFile'],
    ),
    (
        'Prefetch (PECmd)',
        ['Note', 'SourceFilename', 'SourceCreated', 'SourceModified', 'SourceAccessed', 'ExecutableName', 'Hash', 'Size', 'Version', 'RunCount', 'LastRun', 'PreviousRun0', 'PreviousRun1', 'PreviousRun2', 'PreviousRun3', 'PreviousRun4', 'PreviousRun5', 'PreviousRun6', 'Volume0Name', 'Volume0Serial', 'Volume0Created', 'Volume1Name', 'Volume1Serial', 'Volume1Created', 'Directories', 'FilesLoaded', 'ParsingError'],
    ),
    (
        'Prefetch timeline (PECmd)',
        ['RunTime', 'ExecutableName'],
    ),
    (
        'Recycle Bin (RBCmd)',
        ['SourceName', 'FileType', 'FileName', 'FileSize', 'DeletedOn'],
    ),
    (
        'Windows Timeline — activity (WxTCmd)',
        ['Id', 'ActivityTypeOrg', 'ActivityType', 'Executable', 'DisplayText', 'ContentInfo', 'Payload', 'ClipboardPayload', 'StartTime', 'EndTime', 'Duration', 'LastModifiedTime', 'LastModifiedOnClient', 'OriginalLastModifiedOnClient', 'ExpirationTime', 'CreatedInCloud', 'IsLocalOnly', 'ETag', 'PackageIdHash', 'PlatformDeviceId', 'DevicePlatform', 'TimeZone'],
    ),
    (
        'Windows Timeline — operations (WxTCmd)',
        ['Id', 'ActivityTypeOrg', 'ActivityType', 'Executable', 'DisplayText', 'ContentInfo', 'Payload', 'ClipboardPayload', 'StartTime', 'EndTime', 'Duration', 'LastModifiedTime', 'LastModifiedTimeOnClient', 'CreatedTime', 'ExpirationTime', 'OperationExpirationTime', 'OperationOrder', 'AppId', 'OperationType', 'Description', 'PlatformDeviceId', 'DevicePlatform', 'TimeZone'],
    ),
    (
        'Windows Timeline — package IDs (WxTCmd)',
        ['Id', 'Platform', 'Name', 'AdditionalInformation', 'Expires'],
    ),
    (
        'Jump lists — automatic (JLECmd)',
        ['SourceFile', 'SourceCreated', 'SourceModified', 'SourceAccessed', 'AppId', 'AppIdDescription', 'DestListVersion', 'LastUsedEntryNumber', 'MRU', 'EntryNumber', 'CreationTime', 'LastModified', 'Hostname', 'MacAddress', 'Path', 'InteractionCount', 'PinStatus', 'FileBirthDroid', 'FileDroid', 'VolumeBirthDroid', 'VolumeDroid', 'TargetCreated', 'TargetModified', 'TargetAccessed', 'FileSize', 'RelativePath', 'WorkingDirectory', 'FileAttributes', 'HeaderFlags', 'DriveType', 'VolumeSerialNumber', 'VolumeLabel', 'LocalPath', 'CommonPath', 'TargetIDAbsolutePath', 'TargetMFTEntryNumber', 'TargetMFTSequenceNumber', 'MachineID', 'MachineMACAddress', 'TrackerCreatedOn', 'ExtraBlocksPresent', 'Arguments', 'Notes'],
    ),
    (
        'Jump lists — custom (JLECmd)',
        ['SourceFile', 'SourceCreated', 'SourceModified', 'SourceAccessed', 'AppId', 'AppIdDescription', 'EntryName', 'TargetCreated', 'TargetModified', 'TargetAccessed', 'FileSize', 'RelativePath', 'WorkingDirectory', 'FileAttributes', 'HeaderFlags', 'DriveType', 'VolumeSerialNumber', 'VolumeLabel', 'LocalPath', 'CommonPath', 'TargetIDAbsolutePath', 'TargetMFTEntryNumber', 'TargetMFTSequenceNumber', 'MachineID', 'MachineMACAddress', 'TrackerCreatedOn', 'ExtraBlocksPresent', 'Arguments'],
    ),
    (
        'LNK files (LECmd)',
        ['SourceFile', 'SourceCreated', 'SourceModified', 'SourceAccessed', 'TargetCreated', 'TargetModified', 'TargetAccessed', 'FileSize', 'RelativePath', 'WorkingDirectory', 'FileAttributes', 'HeaderFlags', 'DriveType', 'VolumeSerialNumber', 'VolumeLabel', 'LocalPath', 'NetworkPath', 'CommonPath', 'Arguments', 'TargetIDAbsolutePath', 'TargetMFTEntryNumber', 'TargetMFTSequenceNumber', 'MachineID', 'MachineMACAddress', 'MACVendor', 'TrackerCreatedOn', 'ExtraBlocksPresent'],
    ),
    (
        'Shellbags (SBECmd)',
        ['BagPath', 'Slot', 'NodeSlot', 'MRUPosition', 'AbsolutePath', 'ShellType', 'Value', 'ChildBags', 'CreatedOn', 'ModifiedOn', 'AccessedOn', 'LastWriteTime', 'MFTEntry', 'MFTSequenceNumber', 'ExtensionBlockCount', 'FirstInteracted', 'LastInteracted', 'HasExplored', 'Miscellaneous'],
    ),
    (
        'NTFS $Boot (MFTECmd)',
        ['EntryPoint', 'Signature', 'BytesPerSector', 'SectorsPerCluster', 'ClusterSize', 'ReservedSectors', 'TotalSectors', 'MftClusterBlockNumber', 'MftMirrClusterBlockNumber', 'MftEntrySize', 'IndexEntrySize', 'VolumeSerialNumberRaw', 'VolumeSerialNumber', 'VolumeSerialNumber32', 'VolumeSerialNumber32Reverse', 'SectorSignature', 'SourceFile'],
    ),
    (
        'NTFS $MFT (MFTECmd)',
        ['EntryNumber', 'SequenceNumber', 'InUse', 'ParentEntryNumber', 'ParentSequenceNumber', 'ParentPath', 'FileName', 'Extension', 'FileSize', 'ReferenceCount', 'ReparseTarget', 'IsDirectory', 'HasAds', 'IsAds', 'SI<FN', 'uSecZeros', 'Copied', 'SiFlags', 'NameType', 'Created0x10', 'Created0x30', 'LastModified0x10', 'LastModified0x30', 'LastRecordChange0x10', 'LastRecordChange0x30', 'LastAccess0x10', 'LastAccess0x30', 'UpdateSequenceNumber', 'LogfileSequenceNumber', 'SecurityId', 'ObjectIdFileDroid', 'LoggedUtilStream', 'ZoneIdContents'],
    ),
    (
        'USN journal $J (MFTECmd)',
        ['Name', 'Extension', 'EntryNumber', 'SequenceNumber', 'ParentEntryNumber', 'ParentSequenceNumber', 'ParentPath', 'UpdateSequenceNumber', 'UpdateTimestamp', 'UpdateReasons', 'FileAttributes', 'OffsetToData', 'SourceFile'],
    ),
    (
        'NTFS $SDS (MFTECmd)',
        ['Hash', 'Id', 'Offset', 'OwnerSid', 'GroupSid', 'Control', 'SaclAceCount', 'UniqueSaclAceTypes', 'DaclAceCount', 'UniqueDaclAceTypes', 'FileOffset', 'SourceFile'],
    ),

    # --- Not extracted from real output: written from the tools' documented
    # --- output shapes, unverified against a live triage (the source triage
    # --- had console logs only for these). A set that never matches a real
    # --- file is inert — it names nothing rather than misnaming something.
    (
        'Registry (RECmd batch)',
        ['HivePath', 'HiveType', 'Description', 'Category', 'KeyPath', 'ValueName',
         'ValueType', 'ValueData', 'ValueData2', 'ValueData3', 'Comment', 'Recursive',
         'Deleted', 'LastWriteTimestamp', 'PluginDetailFile'],
    ),
    (
        'SRUM — app resource use (SrumECmd)',
        ['Id', 'Timestamp', 'ExeInfo', 'ExeInfoDescription', 'ExeTimestamp', 'SidType',
         'Sid', 'UserName', 'UserId', 'AppId', 'BackgroundBytesRead',
         'BackgroundBytesWritten', 'BackgroundContextSwitches', 'BackgroundCycleTime',
         'BackgroundNumberOfFlushes', 'BackgroundNumReadOperations',
         'BackgroundNumWriteOperations', 'FaceTime', 'ForegroundBytesRead',
         'ForegroundBytesWritten', 'ForegroundContextSwitches', 'ForegroundCycleTime',
         'ForegroundNumberOfFlushes', 'ForegroundNumReadOperations',
         'ForegroundNumWriteOperations'],
    ),
    (
        'SRUM — network usage (SrumECmd)',
        ['Id', 'Timestamp', 'ExeInfo', 'ExeInfoDescription', 'ExeTimestamp', 'SidType',
         'Sid', 'UserName', 'UserId', 'AppId', 'BytesReceived', 'BytesSent',
         'InterfaceLuid', 'InterfaceType', 'L2ProfileFlags', 'L2ProfileId',
         'ProfileName'],
    ),
]
