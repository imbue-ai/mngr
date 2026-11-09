"""The Perl a VM runs to publish each agent container's memory headroom into the container's volume.

Shared by the VPS hosts' publisher (``mngr_vps``'s ``host_setup``) and the gen-2
slice guests' (``mngr_imbue_cloud``'s ``gen2_scripts.guest``, which publishes the
disk headroom beside it). It lives here because the gen-2 renderers ship into a
container that holds no mngr package, only this one. perl, because every write
into the container's volume must refuse a symlink the container planted there,
and bash cannot open with O_NOFOLLOW.

A renderer prepends ``MEMINFO_PUBLISHER_PERL_PRAGMAS``, then a header declaring
the script-level variables the subs read (``$cgroup_parent``, the docker cgroup
parent; ``$host_id_label``, the label marking an agent host container;
``$volume_mount_path``, where the container mounts its volume), then
``MEMINFO_PUBLISHER_PERL_SUBS``, then its own main loop over ``discover()``.
"""

from typing import Final

# The file, at the root of the agent container's volume, that holds the published
# headroom; an in-container memory guard such as earlyoom reads it there
# (``--host-meminfo``). One name for every VM-side publisher, since the reader
# inside the container is the same whichever VM runs the container.
CONTAINER_HOST_MEMINFO_FILE_NAME: Final[str] = ".host-meminfo"

MEMINFO_PUBLISHER_PERL_PRAGMAS: Final[str] = """use strict;
use warnings;
use Fcntl qw(O_WRONLY O_CREAT O_EXCL O_NOFOLLOW O_NONBLOCK);
"""

MEMINFO_PUBLISHER_PERL_SUBS: Final[str] = r"""
# docker's output, or undef if it fails. Its stderr is dropped: a container
# that stops mid-discovery fails its inspect.
sub docker_output {
    my $pid = open(my $out, '-|') // return undef;
    if ($pid == 0) {
        open(STDERR, '>', '/dev/null');
        exec('docker', @_) or exit 127;
    }
    my $output = do { local $/; <$out> } // '';
    close($out) or return undef;
    chomp $output;
    return $output;
}

# [cgroup directory, volume directory] for each running agent host container.
# The volume directory is the named volume's bind device, the subvolume itself
# rather than docker's own mount of it.
sub discover {
    my @targets;
    my $ids = docker_output('ps', '-q', '--no-trunc', '--filter', "label=$host_id_label") // return ();
    for my $container_id (split ' ', $ids) {
        my $volume_name = docker_output(
            'inspect', '--format',
            '{{range .Mounts}}{{if eq .Destination "' . $volume_mount_path . '"}}{{.Name}}{{end}}{{end}}',
            $container_id,
        );
        next unless defined $volume_name && $volume_name ne '';
        my $volume_dir = docker_output('volume', 'inspect', '--format', '{{.Options.device}}', $volume_name);
        next unless defined $volume_dir && $volume_dir ne '' && -d $volume_dir;
        push @targets, ["$cgroup_parent/docker-$container_id.scope", $volume_dir];
    }
    return @targets;
}

sub read_first_line {
    my ($path) = @_;
    open(my $fh, '<', $path) or return undef;
    my $line = <$fh>;
    close($fh);
    return undef unless defined $line;
    chomp $line;
    return $line;
}

# Replaces $path by rename from a fresh temp file beside it, created so that
# no planted name can be written through. True once $path holds $content.
sub write_replacing {
    my ($path, $content) = @_;
    my ($fh, $tmp);
    for (1 .. 100) {
        $tmp = sprintf('%s.%08x', $path, int(rand(4294967296)));
        last if sysopen($fh, $tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0644);
        undef $fh;
    }
    return 0 unless defined $fh;
    print {$fh} $content;
    close($fh) or do { unlink $tmp; return 0 };
    return 1 if rename($tmp, $path);
    unlink $tmp;
    return 0;
}

# Overwrites $path in place, so it keeps its inode. Only a regular file of
# exactly the content's length is overwritten; anything else (the first write,
# an older format, a planted symlink or fifo) is replaced by rename. True once
# $path holds $content.
sub write_in_place {
    my ($path, $content) = @_;
    if (sysopen(my $fh, $path, O_WRONLY | O_NOFOLLOW | O_NONBLOCK)) {
        if (-f $fh && -s _ == length($content)) {
            my $written = syswrite($fh, $content);
            close($fh);
            return 1 if defined $written && $written == length($content);
        } else {
            close($fh);
        }
    }
    return write_replacing($path, $content);
}

# Points $path at a complete copy of $content while the sandbox only ever sees
# two inodes. gVisor's gofer keeps an entry for every distinct inode the sandbox
# sees, for its lifetime, so replacing $path with a fresh file on every rewrite
# grows it without bound; overwriting $path itself would let a reader catch a
# half-written file. Instead the content is written in place into whichever of
# two slot files $path does not name, which is then renamed onto $path through
# a hard link.
sub publish_through_slot {
    my ($path, $content) = @_;
    my @published = lstat($path);
    my @slot0 = lstat("$path.slot0");
    my $slot = @published && @slot0 && $published[0] == $slot0[0] && $published[1] == $slot0[1] ? 1 : 0;
    my $slot_path = "$path.slot$slot";
    write_in_place($slot_path, $content) or return;
    my $link;
    for (1 .. 100) {
        $link = sprintf('%s.%08x', $path, int(rand(4294967296)));
        last if link($slot_path, $link);
        return unless $!{EEXIST};
        undef $link;
    }
    return unless defined $link;
    # A rename onto a name for the same file does nothing and leaves $link behind.
    rename($link, $path);
    unlink $link;
}

sub withdraw {
    my ($target_file) = @_;
    unlink $target_file, "$target_file.slot0", "$target_file.slot1";
}

sub publish_memory {
    my ($cgroup_dir, $target_file) = @_;
    # A container can stop, and its cgroup vanish, between any two reads.
    my $limit = read_first_line("$cgroup_dir/memory.max") // return;
    if ($limit eq 'max') {
        withdraw($target_file);
        return;
    }
    return unless $limit =~ /^\d+$/;
    my $usage = read_first_line("$cgroup_dir/memory.current") // return;
    return unless $usage =~ /^\d+$/;
    open(my $stat, '<', "$cgroup_dir/memory.stat") or return;
    my $inactive_file = 0;
    while (my $line = <$stat>) {
        my ($key, $value) = split ' ', $line;
        next unless defined $value && $value =~ /^\d+$/;
        $inactive_file = $value if $key eq 'inactive_file';
    }
    close($stat);
    my $available = $limit - $usage + $inactive_file;
    $available = 0 if $available < 0;
    $available = $limit if $available > $limit;
    # Fixed-width fields, so every rewrite of a slot has the same length (see write_in_place).
    publish_through_slot(
        $target_file,
        sprintf(
            "MemTotal: %15d kB\nMemAvailable: %15d kB\nTimestamp: %15d\n",
            $limit / 1024, $available / 1024, time,
        ),
    );
}
"""
