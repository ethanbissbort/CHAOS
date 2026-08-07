using Chaos.Host.Configuration;
using Chaos.Host.Http;
using Chaos.Host.Setup;
using Chaos.Host.Tests.Support;
using Microsoft.Extensions.Primitives;

namespace Chaos.Host.Tests;

/// <summary>
/// The three things setup reads from outside itself: the platform's status
/// payload, the design package on disk, and the caller's <c>force</c> flag.
/// </summary>
public sealed class SetupInputTests
{
    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("not json at all")]
    [InlineData("[1, 2, 3]")]
    public void Output_that_is_not_a_status_payload_is_rejected_with_a_reason(string output)
    {
        Assert.False(PlatformStatusReading.TryParse(output, out var reading, out var problem));
        Assert.Null(reading);
        Assert.False(string.IsNullOrWhiteSpace(problem));
    }

    [Fact]
    public void A_banner_before_the_payload_does_not_stop_it_being_read()
    {
        var output = "warning: could not import the simulator\n" + PlatformStatusJson.Loaded();

        Assert.True(PlatformStatusReading.TryParse(output, out var reading, out var problem), problem);
        Assert.Equal(90, reading!.Count("assets"));
    }

    [Fact]
    public void A_table_that_is_absent_reads_as_unknown_rather_than_zero()
    {
        Assert.True(PlatformStatusReading.TryParse(PlatformStatusJson.Fresh(), out var reading, out _));

        Assert.Null(reading!.Count("assets"));
        Assert.Equal(0, reading.TotalRows);
        Assert.False(reading.SchemaComplete);
        Assert.NotEmpty(reading.MissingTables);
        Assert.True(reading.DatabaseReachable);
    }

    [Fact]
    public void A_sqlite_url_is_resolved_to_a_file_and_a_server_url_is_not()
    {
        var missing = Path.Combine(Path.GetTempPath(), "chaos-tests-" + Guid.NewGuid().ToString("N"), "x.db");
        var file = DatabaseFileFacts.Inspect("sqlite:///" + missing);

        Assert.True(file.IsFile);
        Assert.False(file.FileExists);
        Assert.Contains("does not exist yet", file.Describe(), StringComparison.Ordinal);

        var server = DatabaseFileFacts.Inspect("postgresql://chaos:***@db:5432/chaos");
        Assert.False(server.IsFile);
        Assert.Null(server.FileExists);

        Assert.Null(DatabaseFileFacts.Inspect(null).Url);
        Assert.False(DatabaseFileFacts.Inspect("sqlite:///:memory:").IsFile);
    }

    [Fact]
    public async Task The_design_package_fingerprint_follows_the_contents_of_data()
    {
        var directory = Path.Combine(Path.GetTempPath(), "chaos-data-tests-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);

        try
        {
            var empty = DesignPackageFacts.Inspect(directory, "configured");
            Assert.True(empty.Exists);
            Assert.Equal(0, empty.DocumentCount);
            Assert.Null(empty.Fingerprint);

            await File.WriteAllTextAsync(Path.Combine(directory, "assets.yaml"), "assets: []\n");
            await File.WriteAllTextAsync(Path.Combine(directory, "points.yaml"), "points: []\n");

            // The JSON mirrors in data/ are deliberately not hashed: the loader
            // reads the YAML, and hashing both would report every change twice.
            await File.WriteAllTextAsync(Path.Combine(directory, "assets.json"), "{}");

            var loaded = DesignPackageFacts.Inspect(directory, "configured");
            Assert.Equal(2, loaded.DocumentCount);
            Assert.StartsWith("sha256:", loaded.Fingerprint!, StringComparison.Ordinal);

            Assert.Equal(loaded.Fingerprint, DesignPackageFacts.Inspect(directory, "configured").Fingerprint);

            await File.WriteAllTextAsync(Path.Combine(directory, "points.yaml"), "points: [one]\n");
            Assert.NotEqual(loaded.Fingerprint, DesignPackageFacts.Inspect(directory, "configured").Fingerprint);
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void An_absent_data_directory_is_reported_as_absent_and_an_unknown_one_as_unknown()
    {
        var absent = DesignPackageFacts.Inspect(
            Path.Combine(Path.GetTempPath(), "chaos-absent-" + Guid.NewGuid().ToString("N")), "configured");
        Assert.False(absent.Exists);
        Assert.Null(absent.Problem);

        var unknown = DesignPackageFacts.Inspect(null, "derived");
        Assert.Equal("unknown", unknown.Source);
        Assert.Null(unknown.DocumentCount);
        Assert.False(string.IsNullOrWhiteSpace(unknown.Problem));
    }

    [Theory]
    [InlineData("true", true)]
    [InlineData("TRUE", true)]
    [InlineData("1", true)]
    [InlineData("yes", true)]
    [InlineData("on", true)]
    [InlineData("", true)]         // "?force" with no value: present, so meant.
    [InlineData("false", false)]
    [InlineData("0", false)]
    [InlineData("maybe", false)]
    public void Only_an_explicit_affirmative_counts_as_force(string value, bool expected) =>
        Assert.Equal(expected, QueryFlags.IsTrue(new StringValues(value)));

    [Fact]
    public void An_absent_force_flag_is_never_consent() =>
        Assert.False(QueryFlags.IsTrue(StringValues.Empty));

    [Fact]
    public void Setup_timeouts_are_validated_before_anything_launches()
    {
        var validator = new ChaosHostOptionsValidator();

        var backwards = Valid();
        backwards.SetupTimeout = TimeSpan.FromMinutes(1);
        backwards.SetupCommandTimeout = TimeSpan.FromMinutes(10);

        var result = validator.Validate(name: null, backwards);
        Assert.True(result.Failed);
        Assert.Contains("SetupTimeout", result.FailureMessage, StringComparison.Ordinal);

        var zeroed = Valid();
        zeroed.SetupProbeTimeout = TimeSpan.Zero;
        Assert.True(validator.Validate(name: null, zeroed).Failed);

        Assert.True(validator.Validate(name: null, Valid()).Succeeded);
    }

    [Fact]
    public void Auto_setup_is_on_by_default_so_a_fresh_install_needs_no_terminal() =>
        Assert.True(new ChaosHostOptions().AutoSetup);

    [Fact]
    public void The_database_and_data_directory_default_to_the_platforms_own_answer()
    {
        var options = new ChaosHostOptions();

        // Empty means "do not override, and report whatever the platform says
        // it used" rather than a path the gateway guessed.
        Assert.Null(options.DatabaseUrl);
        Assert.Null(options.DataDirectory);
    }

    private static ChaosHostOptions Valid() => new();
}
