using Chaos.Shell.Core;
using Xunit;

namespace Chaos.Shell.Core.Tests;

/// <summary>
/// The property under test is a preference, not a lookup: the help window must
/// prefer a file on disk over anything the platform serves, because help is
/// most needed when the platform is down.
/// </summary>
public sealed class HelpLocatorTests
{
    private const string AppDir = @"C:\Program Files\Project CHAOS";
    private static readonly Uri DocsPort = new("http://127.0.0.1:8090/");
    private static readonly HostEndpoints Endpoints = HostEndpoints.Default;

    private static Func<string, bool> Only(params string[] existing) =>
        path => existing.Any(e => path.EndsWith(e, StringComparison.OrdinalIgnoreCase));

    [Fact]
    public void A_local_file_beats_the_documentation_port()
    {
        var result = HelpLocator.Resolve(
            AppDir,
            Only(@"web\docs\chaos-help-offline.html"),
            DocsPort,
            Endpoints,
            gatewayReachable: true);

        Assert.Equal(HelpSourceKind.LocalFile, result.Kind);
        Assert.True(result.WorksOffline);
        Assert.Equal(Uri.UriSchemeFile, result.Target!.Scheme);
    }

    [Fact]
    public void The_documentation_port_is_used_when_no_file_is_installed()
    {
        var result = HelpLocator.Resolve(
            AppDir, _ => false, DocsPort, Endpoints, gatewayReachable: true);

        Assert.Equal(HelpSourceKind.DocumentationPort, result.Kind);
        Assert.Equal(DocsPort, result.Target);
        Assert.False(result.WorksOffline);
    }

    [Fact]
    public void The_gateway_copy_is_the_last_resort()
    {
        var result = HelpLocator.Resolve(
            AppDir, _ => false, documentationUrl: null, Endpoints, gatewayReachable: true);

        Assert.Equal(HelpSourceKind.GatewayServed, result.Kind);
        Assert.EndsWith("ui/docs/", result.Target!.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void A_served_source_is_never_offered_when_the_gateway_is_silent()
    {
        // Pointing the help window at a URL known to be dead produces exactly
        // the blank window this type exists to prevent.
        var result = HelpLocator.Resolve(
            AppDir, _ => false, DocsPort, Endpoints, gatewayReachable: false);

        Assert.Equal(HelpSourceKind.Missing, result.Kind);
        Assert.Null(result.Target);
    }

    [Fact]
    public void A_local_file_is_still_used_when_the_platform_is_down()
    {
        var result = HelpLocator.Resolve(
            AppDir,
            Only(@"app\web\docs\chaos-help-offline.html"),
            documentationUrl: null,
            Endpoints,
            gatewayReachable: false);

        Assert.Equal(HelpSourceKind.LocalFile, result.Kind);
        Assert.True(result.WorksOffline);
    }

    [Fact]
    public void Every_candidate_is_reported_so_a_missing_install_can_be_diagnosed()
    {
        var result = HelpLocator.Resolve(
            AppDir, _ => false, documentationUrl: null, Endpoints, gatewayReachable: false);

        Assert.Equal(HelpLocator.LocalCandidates.Length + 1, result.Searched.Count);
        Assert.All(result.Searched, entry => Assert.False(string.IsNullOrWhiteSpace(entry)));
        Assert.Contains("Chaos.Runtime", result.Summary, StringComparison.Ordinal);
    }

    [Fact]
    public void Only_a_local_file_ever_claims_to_work_offline()
    {
        foreach (var reachable in new[] { true, false })
        {
            foreach (var docs in new Uri?[] { DocsPort, null })
            {
                var result = HelpLocator.Resolve(AppDir, _ => false, docs, Endpoints, reachable);
                Assert.False(result.WorksOffline);
            }
        }
    }
}
