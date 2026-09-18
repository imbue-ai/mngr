Feature: Listing a directory
  A listing reports what one directory on the addressed machine contains, one entry per member.

  @base-directory-default
  Scenario: A listing with no path lists the base directory itself
    Given a target that fixes a base directory
    When a listing is invoked with no path
    Then the base directory itself is listed

  @named-directory
  Scenario: A listing with a path lists that directory
    Given a target that fixes a base directory
    When a listing is invoked with a relative path naming a directory beneath it
    Then that directory is listed

  @shallow-by-default
  Scenario: A listing reports one level unless descent is requested
    Given a directory holding a file and a subdirectory that itself holds a file
    When it is listed without requesting descent
    Then the file and the subdirectory are reported
    And the file inside the subdirectory is not reported

  @recursive-descent
  Scenario: A listing can descend into subdirectories
    Given a directory holding a file and a subdirectory that itself holds a file
    When it is listed with descent requested
    Then the file, the subdirectory, and the file inside the subdirectory are all reported

  @absolute-entry-paths
  Scenario: Every entry carries its absolute path
    An entry's path is what makes it addressable by another subcommand without the user reassembling it.

    Given a directory holding a file
    When it is listed
    Then the entry for that file carries its absolute path on the machine

  @directories-have-no-size
  Scenario: A directory entry reports no size
    Given a directory holding a file and a subdirectory
    When it is listed
    Then the file's entry reports the file's size in bytes
    And the subdirectory's entry reports no size

  @default-columns
  Scenario: A listing displays a default selection of attributes
    Given a directory holding a file
    When it is listed for a human reader without choosing attributes
    Then each entry displays its name, its kind, its size, and when it was last modified

  @chosen-columns
  Scenario: The displayed attributes can be chosen
    Given a directory holding a file
    When it is listed choosing only the name and the permissions
    Then each entry displays only its name and its permissions

  @template-per-entry
  Scenario: A listing can be rendered through a caller-supplied template
    Given a directory holding one file
    When it is listed with a template naming the entry's name and size
    Then one line is emitted, carrying that file's name and size as the template arranged them

  @template-reaches-every-attribute
  Scenario: A template can name any attribute, not only the displayed ones
    Which attributes are displayed and which a template names are separate choices, so a template that could reach only the displayed ones would make the two interfere.

    Given a directory holding one file
    When it is listed with a template naming an attribute that the default display omits
    Then the emitted line carries that attribute's value

  @unknown-column
  Scenario: Asking for an attribute that does not exist is a usage error
    When a listing is invoked choosing an attribute that is not one an entry carries
    Then the command refuses as a usage error
    And it names the attributes that can be chosen

  @empty-directory
  Scenario: A directory holding nothing is a successful listing of nothing
    Given a directory that exists and holds nothing
    When it is listed
    Then the command succeeds
    And it reports that the directory holds nothing

  @missing-directory
  Scenario: A path where no directory exists is refused
    An empty answer here would be a false statement about the machine, indistinguishable from the truthful empty answer above.

    When a listing addresses a path where no directory exists
    Then the command refuses and names the absolute path it could not list
    And the refusal is distinguishable from a listing of a directory holding nothing
