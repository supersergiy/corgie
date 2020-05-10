import copy
from corgie.cli.downsample import downsample, test

# To add new commands, create a file in this folder implementing a command,
# import the command here and add it to the list:
COMMAND_LIST = [test, downsample]

def get_command_list():
    return copy.deepcopy(COMMAND_LIST)
