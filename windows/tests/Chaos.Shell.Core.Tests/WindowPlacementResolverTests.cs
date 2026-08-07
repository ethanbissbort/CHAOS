using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

/// <summary>
/// Restore rules for a shell whose second window commonly lives on a wall
/// display that gets switched off, unplugged or moved.
/// </summary>
public sealed class WindowPlacementResolverTests
{
    private static readonly ScreenRect DefaultSize = new(0, 0, 1280, 800);

    /// <summary>Primary 1920x1080 at origin, with a 40 px taskbar.</summary>
    private static MonitorInfo Primary => new(
        MonitorKey.For(new ScreenRect(0, 0, 1920, 1080)),
        new ScreenRect(0, 0, 1920, 1040),
        IsPrimary: true,
        Index: 1);

    /// <summary>A 4K wall display to the right of the primary.</summary>
    private static MonitorInfo Wall => new(
        MonitorKey.For(new ScreenRect(1920, 0, 3840, 2160)),
        new ScreenRect(1920, 0, 3840, 2160),
        IsPrimary: false,
        Index: 2);

    /// <summary>A monitor to the LEFT of the primary: negative coordinates.</summary>
    private static MonitorInfo LeftHand => new(
        MonitorKey.For(new ScreenRect(-1920, 0, 1920, 1080)),
        new ScreenRect(-1920, 0, 1920, 1040),
        IsPrimary: false,
        Index: 3);

    // ------------------------------------------------------------------------
    // The rule that matters: never restore onto a monitor that is gone.
    // ------------------------------------------------------------------------

    [Fact]
    public void Window_is_never_restored_onto_a_monitor_that_no_longer_exists()
    {
        // The annunciator was full-screen on the wall display. The wall display
        // has been unplugged. Its coordinates are now empty space.
        var saved = WindowPlacement.FromBounds(
            new ScreenRect(1920, 0, 3840, 2160),
            Wall.DeviceId);

        var resolution = WindowPlacementResolver.Resolve(
            saved,
            new[] { Primary },
            DefaultSize);

        Assert.Equal(PlacementAdjustment.MonitorMissing, resolution.Adjustment);
        Assert.Equal(Primary.DeviceId, resolution.Monitor.DeviceId);

        // And it must land somewhere actually visible on the survivor.
        Assert.True(
            Primary.WorkArea.Contains(resolution.Bounds),
            $"restored bounds {resolution.Bounds} are not inside {Primary.WorkArea}");
    }

    [Fact]
    public void Restored_bounds_are_always_within_the_chosen_work_area()
    {
        // Sweep a range of saved rectangles, including ones far off the desktop
        // in every direction, and assert the invariant holds for all of them.
        var monitors = new[] { Primary, Wall };

        foreach (var saved in new[]
        {
            new ScreenRect(-9000, -9000, 1000, 700),
            new ScreenRect(99999, 99999, 1000, 700),
            new ScreenRect(1900, -500, 1000, 700),
            new ScreenRect(0, 1030, 1000, 700),
            new ScreenRect(5000, 2000, 4000, 3000),
        })
        {
            var resolution = WindowPlacementResolver.Resolve(
                WindowPlacement.FromBounds(saved),
                monitors,
                DefaultSize);

            Assert.True(
                resolution.Monitor.WorkArea.Contains(resolution.Bounds),
                $"saved {saved} resolved to {resolution.Bounds}, outside {resolution.Monitor.WorkArea}");
        }
    }

    [Fact]
    public void Monitor_still_attached_restores_exactly_as_saved()
    {
        var saved = WindowPlacement.FromBounds(
            new ScreenRect(2200, 300, 2400, 1400),
            Wall.DeviceId);

        var resolution = WindowPlacementResolver.Resolve(
            saved,
            new[] { Primary, Wall },
            DefaultSize);

        Assert.Equal(PlacementAdjustment.None, resolution.Adjustment);
        Assert.False(resolution.WasAdjusted);
        Assert.Equal(Wall.DeviceId, resolution.Monitor.DeviceId);
        Assert.Equal(new ScreenRect(2200, 300, 2400, 1400), resolution.Bounds);
    }

    [Fact]
    public void Negative_coordinates_survive_when_the_left_hand_monitor_is_present()
    {
        // A monitor left of the primary is an ordinary layout, not corruption.
        var saved = WindowPlacement.FromBounds(
            new ScreenRect(-1500, 100, 1200, 800),
            LeftHand.DeviceId);

        var resolution = WindowPlacementResolver.Resolve(
            saved,
            new[] { Primary, LeftHand },
            DefaultSize);

        Assert.Equal(PlacementAdjustment.None, resolution.Adjustment);
        Assert.Equal(-1500, resolution.Bounds.Left);
        Assert.Equal(LeftHand.DeviceId, resolution.Monitor.DeviceId);
    }

    [Fact]
    public void Resolution_drop_on_the_same_monitor_clamps_the_window_back_in()
    {
        // The wall display is still there but has been reconfigured from 4K to
        // 1080p. The saved rectangle no longer fits.
        var saved = WindowPlacement.FromBounds(
            new ScreenRect(1920, 0, 3840, 2160),
            Wall.DeviceId);

        var shrunkWall = new MonitorInfo(
            Wall.DeviceId,
            new ScreenRect(1920, 0, 1920, 1080),
            IsPrimary: false,
            Index: 2);

        var resolution = WindowPlacementResolver.Resolve(
            saved,
            new[] { Primary, shrunkWall },
            DefaultSize);

        Assert.Equal(PlacementAdjustment.ClampedToWorkArea, resolution.Adjustment);
        Assert.True(shrunkWall.WorkArea.Contains(resolution.Bounds));
        Assert.Equal(1920, resolution.Bounds.Width);
        Assert.Equal(1080, resolution.Bounds.Height);
    }

    [Fact]
    public void Caption_is_kept_reachable_when_the_window_was_saved_above_the_screen()
    {
        // A window whose title bar sits above the top edge cannot be dragged
        // back with a mouse.
        var saved = WindowPlacement.FromBounds(
            new ScreenRect(200, -400, 1000, 700),
            Primary.DeviceId);

        var resolution = WindowPlacementResolver.Resolve(
            saved,
            new[] { Primary },
            DefaultSize);

        Assert.True(resolution.Bounds.Top >= Primary.WorkArea.Top);
        Assert.True(resolution.WasAdjusted);
    }

    [Fact]
    public void Absurdly_small_saved_size_is_raised_to_a_usable_minimum()
    {
        var saved = WindowPlacement.FromBounds(
            new ScreenRect(100, 100, 8, 6),
            Primary.DeviceId);

        var resolution = WindowPlacementResolver.Resolve(
            saved,
            new[] { Primary },
            DefaultSize,
            minimumWidth: 640,
            minimumHeight: 480);

        Assert.True(resolution.Bounds.Width >= 640);
        Assert.True(resolution.Bounds.Height >= 480);
    }

    [Fact]
    public void No_saved_placement_centres_on_the_primary()
    {
        var resolution = WindowPlacementResolver.Resolve(
            saved: null,
            new[] { Primary, Wall },
            DefaultSize);

        Assert.Equal(PlacementAdjustment.Defaulted, resolution.Adjustment);
        Assert.Equal(Primary.DeviceId, resolution.Monitor.DeviceId);
        Assert.Equal((1920 - 1280) / 2, resolution.Bounds.Left);
        Assert.Equal((1040 - 800) / 2, resolution.Bounds.Top);
    }

    [Fact]
    public void Empty_saved_rectangle_is_treated_as_no_placement()
    {
        var resolution = WindowPlacementResolver.Resolve(
            WindowPlacement.FromBounds(new ScreenRect(10, 10, 0, 0)),
            new[] { Primary },
            DefaultSize);

        Assert.Equal(PlacementAdjustment.Defaulted, resolution.Adjustment);
    }

    [Fact]
    public void No_monitors_at_all_does_not_throw()
    {
        // Reported during a display driver reset or an RDP session teardown.
        var resolution = WindowPlacementResolver.Resolve(
            WindowPlacement.FromBounds(new ScreenRect(0, 0, 900, 600), "\\\\.\\DISPLAY7"),
            Array.Empty<MonitorInfo>(),
            DefaultSize);

        Assert.Equal(PlacementAdjustment.Defaulted, resolution.Adjustment);
        Assert.Equal(DefaultSize, resolution.Bounds);
    }

    [Fact]
    public void Maximized_state_is_preserved_across_a_relocation()
    {
        var saved = WindowPlacement.FromBounds(
            new ScreenRect(1920, 0, 3840, 2160),
            Wall.DeviceId,
            maximized: true);

        var resolution = WindowPlacementResolver.Resolve(saved, new[] { Primary }, DefaultSize);

        Assert.True(resolution.Maximized);
        Assert.Equal(PlacementAdjustment.MonitorMissing, resolution.Adjustment);
    }

    // ------------------------------------------------------------------------
    // Explicit monitor selection, for --monitor / "open annunciator there".
    // ------------------------------------------------------------------------

    [Theory]
    [InlineData("2", 2)]
    [InlineData("3", 3)]
    [InlineData("primary", 1)]
    [InlineData("PRIMARY", 1)]
    [InlineData(null, 1)]
    [InlineData("", 1)]
    public void Monitor_selection_by_index_or_name(string? request, int expectedIndex)
    {
        var monitors = new[] { Primary, Wall, LeftHand };

        var chosen = WindowPlacementResolver.Select(monitors, request);

        Assert.Equal(expectedIndex, chosen.Index);
    }

    [Fact]
    public void Out_of_range_monitor_request_falls_back_to_primary_rather_than_failing()
    {
        // A shortcut says --monitor 4 but someone unplugged a screen. Opening
        // on the wrong display is recoverable; not opening is not.
        var chosen = WindowPlacementResolver.Select(new[] { Primary, Wall }, "4");

        Assert.Equal(Primary.DeviceId, chosen.DeviceId);
    }

    [Fact]
    public void Unknown_monitor_name_falls_back_to_primary()
    {
        var chosen = WindowPlacementResolver.Select(new[] { Primary, Wall }, "\\\\.\\DISPLAY9");

        Assert.Equal(Primary.DeviceId, chosen.DeviceId);
    }

    [Fact]
    public void Monitor_can_be_selected_by_its_persisted_key()
    {
        var chosen = WindowPlacementResolver.Select(new[] { Primary, Wall }, Wall.DeviceId);

        Assert.Equal(Wall.DeviceId, chosen.DeviceId);
        Assert.Equal(2, chosen.Index);
    }

    [Fact]
    public void Selection_with_no_monitors_throws_rather_than_inventing_one()
    {
        Assert.Throws<ArgumentException>(
            () => WindowPlacementResolver.Select(Array.Empty<MonitorInfo>(), "1"));
    }

    // ------------------------------------------------------------------------
    // Geometry helpers.
    // ------------------------------------------------------------------------

    [Fact]
    public void Intersection_area_is_zero_for_disjoint_rectangles()
    {
        var a = new ScreenRect(0, 0, 100, 100);
        var b = new ScreenRect(200, 200, 100, 100);

        Assert.Equal(0, a.IntersectionArea(b));
        Assert.Equal(0, b.IntersectionArea(a));
    }

    [Fact]
    public void Intersection_area_is_symmetric_and_correct()
    {
        var a = new ScreenRect(0, 0, 100, 100);
        var b = new ScreenRect(50, 50, 100, 100);

        Assert.Equal(2500, a.IntersectionArea(b));
        Assert.Equal(2500, b.IntersectionArea(a));
    }

    [Fact]
    public void Touching_edges_do_not_count_as_overlap()
    {
        var a = new ScreenRect(0, 0, 100, 100);
        var b = new ScreenRect(100, 0, 100, 100);

        Assert.Equal(0, a.IntersectionArea(b));
    }

    [Fact]
    public void Monitor_key_is_stable_and_distinguishes_geometry()
    {
        var wall = new ScreenRect(1920, 0, 3840, 2160);

        Assert.Equal(MonitorKey.For(wall), MonitorKey.For(wall));
        Assert.NotEqual(MonitorKey.For(wall), MonitorKey.For(new ScreenRect(0, 0, 3840, 2160)));
        Assert.NotEqual(MonitorKey.For(wall), MonitorKey.For(new ScreenRect(1920, 0, 1920, 1080)));
    }
}
