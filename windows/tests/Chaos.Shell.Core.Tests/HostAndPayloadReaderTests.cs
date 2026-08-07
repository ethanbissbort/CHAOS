using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

public sealed class HostEndpointsTests
{
    [Fact]
    public void Default_points_at_the_local_gateway()
    {
        var endpoints = HostEndpoints.Default;

        Assert.Equal("http://127.0.0.1:8080/", endpoints.BaseUri.ToString());
        Assert.True(endpoints.IsLoopback);
    }

    [Fact]
    public void Paths_match_how_the_platform_serves_them()
    {
        var endpoints = HostEndpoints.For(new Uri("http://node:8080"));

        Assert.Equal("http://node:8080/", endpoints.Console.ToString());
        Assert.Equal("http://node:8080/ui/annunciator.html", endpoints.Annunciator.ToString());
        Assert.Equal("http://node:8080/health", endpoints.Health.ToString());
        Assert.Equal("http://node:8080/api/v1/alarms/active", endpoints.ActiveAlarms.ToString());
        Assert.Equal("http://node:8080/docs", endpoints.ApiDocs.ToString());
    }

    [Fact]
    public void A_base_path_prefix_is_preserved_for_reverse_proxies()
    {
        // The web assets resolve their own root, so a prefixed deployment works
        // as long as the shell does not throw the prefix away.
        var endpoints = HostEndpoints.For(new Uri("https://gw.example/chaos"));

        Assert.Equal("https://gw.example/chaos/", endpoints.Console.ToString());
        Assert.Equal("https://gw.example/chaos/ui/annunciator.html", endpoints.Annunciator.ToString());
        Assert.Equal("https://gw.example/chaos/health", endpoints.Health.ToString());
    }

    [Fact]
    public void Lan_url_swaps_the_host_and_keeps_the_port()
    {
        var lan = HostEndpoints.Default.LanUrlFor("chaos-node");

        Assert.Equal("http://chaos-node:8080/", lan.ToString());
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    public void Lan_url_with_no_hostname_falls_back_to_the_base(string? host)
    {
        Assert.Equal(HostEndpoints.Default.BaseUri, HostEndpoints.Default.LanUrlFor(host!));
    }

    [Fact]
    public void Relative_base_addresses_are_rejected()
    {
        Assert.Throws<ArgumentException>(
            () => HostEndpoints.For(new Uri("/chaos", UriKind.Relative)));
    }
}

public sealed class HostUrlResolverTests
{
    [Fact]
    public void Nothing_configured_uses_the_loopback_default()
    {
        var resolution = HostUrlResolver.Resolve(null, null);

        Assert.Equal(HostUrlSource.Default, resolution.Source);
        Assert.Equal(HostEndpoints.Default.BaseUri, resolution.Endpoints.BaseUri);
        Assert.Null(resolution.Problem);
        Assert.False(resolution.UsedFallback);
    }

    [Fact]
    public void Command_line_beats_the_environment()
    {
        var resolution = HostUrlResolver.Resolve("http://cli:9000", "http://env:9100");

        Assert.Equal(HostUrlSource.CommandLine, resolution.Source);
        Assert.Equal("http://cli:9000/", resolution.Endpoints.BaseUri.ToString());
    }

    [Fact]
    public void The_environment_is_used_when_the_command_line_is_silent()
    {
        var resolution = HostUrlResolver.Resolve(null, "http://env:9100");

        Assert.Equal(HostUrlSource.Environment, resolution.Source);
        Assert.Equal("http://env:9100/", resolution.Endpoints.BaseUri.ToString());
    }

    [Theory]
    [InlineData("chaos-node", "http://chaos-node:8080/")]
    [InlineData("chaos-node:9000", "http://chaos-node:9000/")]
    [InlineData("http://chaos-node", "http://chaos-node/")]
    [InlineData("https://gw.example", "https://gw.example/")]
    [InlineData("http://10.0.0.5:8080/", "http://10.0.0.5:8080/")]
    public void Operator_shorthand_is_accepted(string input, string expected)
    {
        Assert.True(HostUrlResolver.TryParse(input, out var uri, out var problem));

        Assert.Null(problem);
        Assert.Equal(expected, HostEndpoints.For(uri!).BaseUri.ToString());
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("file:///c:/somewhere")]
    [InlineData("ftp://node")]
    public void Addresses_the_gateway_cannot_serve_are_refused(string input)
    {
        Assert.False(HostUrlResolver.TryParse(input, out var uri, out var problem));

        Assert.Null(uri);
        Assert.NotNull(problem);
    }

    [Fact]
    public void A_rejected_setting_falls_back_but_says_it_did()
    {
        // Silently ignoring an operator's --host and connecting somewhere else
        // is exactly the kind of quiet substitution this platform forbids.
        var resolution = HostUrlResolver.Resolve("ftp://nope", null);

        Assert.Equal(HostUrlSource.Default, resolution.Source);
        Assert.True(resolution.UsedFallback);
        Assert.NotNull(resolution.Problem);
        Assert.Contains("--host", resolution.Problem!, StringComparison.Ordinal);
        Assert.Contains("ftp", resolution.Problem!, StringComparison.Ordinal);
    }

    [Fact]
    public void A_rejected_environment_value_names_the_variable()
    {
        var resolution = HostUrlResolver.Resolve(null, "ftp://nope");

        Assert.Contains(HostUrlResolver.EnvironmentVariable, resolution.Problem!, StringComparison.Ordinal);
    }
}

public sealed class AlarmSummaryReaderTests
{
    private const string RealisticPayload = """
        {
          "count": 3,
          "states": ["active", "acknowledged", "mitigated"],
          "by_severity": {"critical": 1, "major": 1, "warning": 1},
          "suppressed_count": 1,
          "incident_count": 1,
          "alarms": [
            {"alarm_id": 1, "severity": "critical", "state": "active"},
            {"alarm_id": 2, "severity": "major", "state": "acknowledged"},
            {"alarm_id": 3, "severity": "warning", "state": "active"}
          ]
        }
        """;

    [Fact]
    public void Reads_the_platform_response()
    {
        Assert.True(AlarmSummaryReader.TryRead(RealisticPayload, out var counts, out var problem));

        Assert.Null(problem);
        Assert.Equal(1, counts.Critical);
        Assert.Equal(1, counts.Major);
        Assert.Equal(1, counts.Warning);
        Assert.Equal(3, counts.Total);
        Assert.Equal(1, counts.Suppressed);
        Assert.Equal(2, counts.Unacknowledged);
    }

    [Fact]
    public void A_quiet_site_reads_as_zero_not_as_a_failure()
    {
        const string quiet = """{"count": 0, "by_severity": {}, "suppressed_count": 0, "alarms": []}""";

        Assert.True(AlarmSummaryReader.TryRead(quiet, out var counts, out _));

        Assert.Equal(0, counts.Total);
        Assert.False(counts.Any);
        Assert.Equal(0, counts.Unacknowledged);
    }

    [Fact]
    public void Alarms_the_severity_map_does_not_account_for_are_not_lost()
    {
        // The server says four alarms but only itemises two. The surplus is
        // carried as unclassified so the badge still reads 4.
        const string mismatched = """{"count": 4, "by_severity": {"warning": 2}}""";

        Assert.True(AlarmSummaryReader.TryRead(mismatched, out var counts, out _));

        Assert.Equal(2, counts.Warning);
        Assert.Equal(2, counts.Unclassified);
        Assert.Equal(4, counts.Total);
    }

    [Fact]
    public void A_severity_this_build_does_not_know_is_still_counted()
    {
        const string exotic = """{"count": 1, "by_severity": {"catastrophic": 1}}""";

        Assert.True(AlarmSummaryReader.TryRead(exotic, out var counts, out _));

        Assert.Equal(1, counts.Unclassified);
        Assert.Equal(1, counts.Total);
    }

    [Fact]
    public void Missing_alarm_list_leaves_acknowledgement_unknown_not_zero()
    {
        const string summaryOnly = """{"count": 2, "by_severity": {"major": 2}}""";

        Assert.True(AlarmSummaryReader.TryRead(summaryOnly, out var counts, out _));

        Assert.Null(counts.Unacknowledged);
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("not json")]
    [InlineData("[1,2,3]")]
    [InlineData("\"a string\"")]
    [InlineData("{\"count\": 3}")]
    public void An_unreadable_payload_fails_loudly_rather_than_returning_zero(string json)
    {
        // Returning AlarmCounts.None here would put "no active alarms" on the
        // tray off the back of a response we could not read.
        Assert.False(AlarmSummaryReader.TryRead(json, out var counts, out var problem));

        Assert.NotNull(problem);
        Assert.Equal(AlarmCounts.None, counts);
    }

    [Fact]
    public void Non_numeric_severity_values_are_ignored_without_throwing()
    {
        const string odd = """{"count": 1, "by_severity": {"major": "two", "warning": 1}}""";

        Assert.True(AlarmSummaryReader.TryRead(odd, out var counts, out _));

        Assert.Equal(1, counts.Warning);
        Assert.Equal(0, counts.Major);
    }
}

public sealed class AlarmCountsTests
{
    [Fact]
    public void Severity_map_is_case_insensitive()
    {
        var counts = AlarmCounts.FromSeverityMap(
            new Dictionary<string, int> { ["CRITICAL"] = 1, ["Warning"] = 2 });

        Assert.Equal(1, counts.Critical);
        Assert.Equal(2, counts.Warning);
    }

    [Fact]
    public void Worst_severity_is_ordered_correctly()
    {
        Assert.Null(AlarmCounts.None.Worst);
        Assert.Equal(AlarmSeverity.Warning, new AlarmCounts { Warning = 1 }.Worst);
        Assert.Equal(AlarmSeverity.Major, new AlarmCounts { Warning = 9, Major = 1 }.Worst);
        Assert.Equal(AlarmSeverity.Critical, new AlarmCounts { Major = 9, Critical = 1 }.Worst);
        Assert.Equal(AlarmSeverity.Emergency, new AlarmCounts { Critical = 9, Emergency = 1 }.Worst);
    }

    [Fact]
    public void Descriptions_read_worst_first()
    {
        var counts = new AlarmCounts { Warning = 3, Emergency = 1, Major = 2 };

        Assert.Equal("1 emergency, 2 major, 3 warning", counts.Describe());
    }

    [Fact]
    public void An_empty_description_is_left_to_the_caller_to_word()
    {
        Assert.Equal(string.Empty, AlarmCounts.None.Describe());
    }

    [Fact]
    public void Negative_severity_counts_are_discarded()
    {
        var counts = AlarmCounts.FromSeverityMap(
            new Dictionary<string, int> { ["major"] = -5, ["warning"] = 1 });

        Assert.Equal(0, counts.Major);
        Assert.Equal(1, counts.Total);
    }
}

public sealed class PlatformHealthTests
{
    [Fact]
    public void Reads_the_platform_health_document()
    {
        const string json = """
            {"status":"ok","version":"0.4.0","node_role":"primary",
             "site_id":"homestead","physical_control_enabled":false}
            """;

        Assert.True(PlatformHealth.TryRead(json, out var health, out var problem));

        Assert.Null(problem);
        Assert.True(health!.IsOk);
        Assert.Equal("primary", health.NodeRole);
        Assert.False(health.PhysicalControlEnabled);
        Assert.Contains("physical control disabled", health.Describe(), StringComparison.Ordinal);
    }

    [Fact]
    public void A_missing_physical_control_flag_reads_as_unknown()
    {
        const string json = """{"status":"ok"}""";

        Assert.True(PlatformHealth.TryRead(json, out var health, out _));

        Assert.Null(health!.PhysicalControlEnabled);
        Assert.Contains("physical control unknown", health.Describe(), StringComparison.Ordinal);
        Assert.Contains("unknown role", health.Describe(), StringComparison.Ordinal);
    }

    [Fact]
    public void A_degraded_status_is_reported_verbatim_not_normalised()
    {
        const string json = """{"status":"degraded"}""";

        Assert.True(PlatformHealth.TryRead(json, out var health, out _));

        Assert.False(health!.IsOk);
        Assert.Equal("degraded", health.Status);
    }

    [Theory]
    [InlineData("")]
    [InlineData("nonsense")]
    [InlineData("[]")]
    [InlineData("{}")]
    public void An_unreadable_health_document_fails(string json)
    {
        Assert.False(PlatformHealth.TryRead(json, out var health, out var problem));

        Assert.Null(health);
        Assert.NotNull(problem);
    }
}
