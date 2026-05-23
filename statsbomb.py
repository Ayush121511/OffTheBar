from statsbombpy import sb
import pandas as pd
sb.competitions().to_csv('output.csv', index=False)