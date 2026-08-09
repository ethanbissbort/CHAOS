namespace Chaos.Installer;

/// <summary>
/// Nothing uses this type, and nothing should.
///
/// Chaos.Installer is an MSBuild driver: building it runs
/// <c>windows\build\build.ps1 -Task all</c>, which produces the MSI. It is a C#
/// project rather than a <c>.wixproj</c> because Visual Studio cannot open a
/// <c>.wixproj</c> without the WiX extension installed, and a project the IDE
/// refuses to open cannot be built from the IDE at all. The reasoning is in
/// Chaos.Installer.csproj.
///
/// A C# project with no source files at all makes the compiler report "no
/// source files specified", and Directory.Build.props turns warnings into
/// errors — so the project would fail before it ever reached the script it
/// exists to run. One file is cheaper and clearer than special-casing the SDK
/// to skip compilation.
/// </summary>
internal static class InstallerProject
{
}
