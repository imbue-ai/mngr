Feature: Writing a file to a machine
  A write takes content offered locally and makes it the entire content of one addressed file.

  @content-from-input-stream
  Scenario: A write with no named source takes its content from the local input stream
    Given content offered on the local input stream
    When it is written to an addressed path
    Then the addressed file holds exactly that content

  @content-from-local-file
  Scenario: A write can take its content from a named local file
    Given a local file holding known content
    When it is named as the source of a write to an addressed path
    Then the addressed file holds exactly that local file's content

  @source-must-exist
  Scenario: Naming a local source that does not exist is a usage error
    When a write names a local source file that does not exist
    Then the command refuses as a usage error
    And nothing on the addressed machine is written

  @write-reported
  Scenario: A write reports how much landed where
    Given content offered for a write
    When the write succeeds
    Then the command reports the number of bytes written and the absolute path written to

  @write-rendered-through-a-template
  Scenario: A write can be reported through a caller-supplied template
    Given content offered for a write
    When the write succeeds and a template naming the path written and the size is given
    Then one line is emitted, carrying the absolute path written and that size as the template arranged them

  @existing-file-replaced
  Scenario: Writing over an existing file replaces its whole content
    Given an addressed path that already holds a file
    When content is written to that path
    Then the file holds exactly the new content
    And no part of the previous content remains

  @remote-parents-created
  Scenario: Writing creates the directories leading to the addressed path
    Given an addressed path whose parent directories do not exist on the machine
    When content is written to it
    Then those directories are created
    And the addressed file holds exactly that content

  @mode-applied
  Scenario: Requested permissions are applied when the machine can carry them
    Given a running host
    When a file is written to it with requested permissions
    Then the file on the machine carries those permissions

  @no-content-offered
  Scenario: A write with no content available to it is refused with guidance
    A user who invokes a write interactively, naming no source and piping nothing, has not offered content at all; writing an empty file would be a destructive reading of that mistake.

    When a write names no local source and no content is offered on the local input stream
    Then the command refuses and explains both ways of offering content
    And nothing on the addressed machine is written
